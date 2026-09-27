"""Tests that need a database: extractor, loader and motor.

Each test gets a fresh in-memory SQLite with schema + seed.
"""

import re
import sqlite3
from datetime import date, datetime

import pytest

from src import calculo, extractor, loader, motor

AHORA = datetime(2026, 9, 27, 12, 0)


@pytest.fixture
def conn():
    c = extractor.conectar(":memory:")
    extractor.inicializar(c)
    yield c
    c.close()


# ───────────────────────────────────────────────────────────
# extractor
# ───────────────────────────────────────────────────────────

def test_inicializar_carga_el_seed(conn):
    cuenta = lambda t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    assert (cuenta("parametros_ges"), cuenta("parametros_caec"), cuenta("valores_uf"),
            cuenta("beneficiarios"), cuenta("prestaciones")) == (2, 1, 25, 2, 14)


def test_inicializar_sin_seed_deja_tablas_vacias():
    c = extractor.conectar(":memory:")
    extractor.inicializar(c, seed=None)
    assert c.execute("SELECT count(*) FROM prestaciones").fetchone()[0] == 0


def test_conectar_activa_foreign_keys(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_conectar_crea_la_carpeta(tmp_path):
    c = extractor.conectar(tmp_path / "data" / "motor.db")
    c.close()
    assert (tmp_path / "data" / "motor.db").exists()


def test_leer_beneficiario(conn):
    assert extractor.leer_beneficiario(conn, 2) == calculo.Beneficiario(2, 3.5, date(2025, 4, 1))
    assert extractor.leer_beneficiario(conn, 1).fecha_derivacion_caec is None


def test_leer_beneficiario_inexistente_falla(conn):
    with pytest.raises(LookupError):
        extractor.leer_beneficiario(conn, 99)


def test_leer_prestaciones_devuelve_dataclasses_tipadas(conn):
    ps = extractor.leer_prestaciones(conn, 1)
    assert [p.prestacion_id for p in ps] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    p = ps[0]
    assert isinstance(p.fecha_prestacion, date) and isinstance(p.recibido_en, datetime)
    assert p.en_red_ges is True and p.en_red_caec is False


def test_leer_prestaciones_conocido_hasta_excluye_el_dato_tardio(conn):
    ids = [p.prestacion_id for p in extractor.leer_prestaciones(conn, 1, datetime(2026, 3, 4, 23, 59))]
    assert 7 not in ids and 8 in ids


def test_leer_prestaciones_conocido_hasta_es_inclusivo(conn):
    ids = [p.prestacion_id for p in extractor.leer_prestaciones(conn, 1, datetime(2026, 3, 5, 10, 0))]
    assert 7 in ids


def test_leer_prestaciones_rechaza_date_sin_hora(conn):
    with pytest.raises(TypeError):
        extractor.leer_prestaciones(conn, 1, date(2026, 3, 5))


def test_leer_reglas_ges_ordenadas(conn):
    reglas = extractor.leer_reglas_ges(conn, "GES01")
    assert [(r.regla_id, r.tope_uf, r.valid_to) for r in reglas] == [
        (1, 122.0, date(2025, 12, 31)),
        (2, 130.0, None),
    ]


def test_leer_uf(conn):
    assert extractor.leer_uf(conn, date(2025, 2, 28)) == 38_630.75


def test_leer_uf_fecha_faltante_falla(conn):
    with pytest.raises(LookupError):
        extractor.leer_uf(conn, date(2025, 2, 27))


# ───────────────────────────────────────────────────────────
# loader
# ───────────────────────────────────────────────────────────

def _resultado_ben1(conn):
    ps = [p for p in extractor.leer_prestaciones(conn, 1) if p.prestacion_id in (1, 2, 5, 6, 7, 8)]
    filas = calculo.desglose(ps, 4_712_952)
    return calculo.Resultado(
        beneficiario_id=1, tipo="GES", regla_version_id=1,
        ventana_inicio=date(2025, 3, 10), ventana_fin=date(2026, 3, 9),
        acumulado=filas[-1].acumulado, deducible_aplicado=4_712_952,
        fecha_cruce=calculo.fecha_cruce(filas, 4_712_952), filas=filas,
    )


def test_guardar_resultado_escribe_cabecera_y_detalle(conn):
    r = _resultado_ben1(conn)
    cid = loader.guardar_resultado(conn, r, "inicial", AHORA, datetime(2026, 9, 1), "abc123")
    cab = conn.execute("SELECT * FROM calculos_resultado WHERE id = ?", (cid,)).fetchone()
    assert (cab["version_calculo"], cab["vigente"], cab["regla_version_id"], cab["fecha_cruce"]) == (1, 1, 1, "2025-11-20")
    assert (cab["calculado_en"], cab["conocido_hasta"], cab["version_codigo"]) == (
        "2026-09-27T12:00:00", "2026-09-01T00:00:00", "abc123")
    suma, n = conn.execute(
        "SELECT sum(copago), count(*) FROM calculos_detalle WHERE calculo_id = ?", (cid,)
    ).fetchone()
    assert (suma, n) == (cab["acumulado"], 6)


def test_guardar_resultado_es_atomico(conn):
    r = _resultado_ben1(conn)
    loader.guardar_resultado(conn, r, "inicial", AHORA, AHORA, "abc123")
    # a detail row pointing at a prestación that doesn't exist fails the FK mid-transaction
    malo = calculo.Resultado(**{**r.__dict__, "filas": r.filas + (calculo.FilaDesglose(999, date(2025, 12, 1), 1, 1, 0, 1),)})
    with pytest.raises(sqlite3.IntegrityError):
        loader.guardar_resultado(conn, malo, "datos_tardios", AHORA, AHORA, "abc123")
    assert tuple(conn.execute("SELECT count(*), sum(vigente) FROM calculos_resultado").fetchone()) == (1, 1)  # v1 still current
    assert conn.execute("SELECT count(*) FROM calculos_detalle").fetchone()[0] == 6


def test_guardar_resultado_rechaza_conocido_hasta_futuro(conn):
    with pytest.raises(sqlite3.IntegrityError):
        loader.guardar_resultado(conn, _resultado_ben1(conn), "inicial", AHORA, datetime(2027, 1, 1), "abc123")


def test_leer_valores_uf(conn):
    uf = extractor.leer_valores_uf(conn)
    assert len(uf) == 25 and uf[date(2025, 2, 28)] == 38_630.75


def test_leer_resultado_inexistente_falla(conn):
    with pytest.raises(LookupError):
        extractor.leer_resultado(conn, 99)


# ───────────────────────────────────────────────────────────
# motor.calcular — end to end (phase 1 slice)
# ───────────────────────────────────────────────────────────

def test_calcular_ges_de_punta_a_punta(conn):
    cid, _ = motor.calcular(conn, 1, "GES", AHORA, version="abc123")
    r = extractor.leer_resultado(conn, cid)
    assert r["regla_version_id"] == 1                                   # rule in force on ventana_inicio (D1)
    assert (r["ventana_inicio"], r["ventana_fin"]) == ("2025-03-10", "2026-03-09")
    assert r["deducible_aplicado"] == 4_712_952                         # 122 UF × UF(2025-02-28)
    assert r["acumulado"] == 5_900_000
    assert r["fecha_cruce"] == "2025-11-20"
    assert (r["motivo_recalculo"], r["version_calculo"], r["vigente"]) == ("inicial", 1, 1)
    assert (r["conocido_hasta"], r["version_codigo"]) == ("2026-09-27T12:00:00", "abc123")
    # 3 out of network, 4 not GES, 9 after the window
    assert [d["prestacion_id"] for d in r["detalle"]] == [1, 2, 5, 6, 7, 8]
    assert sum(d["copago"] for d in r["detalle"]) == r["acumulado"]
    assert sum(d["paga_beneficiario"] for d in r["detalle"]) == r["deducible_aplicado"]


def test_calcular_con_conocido_hasta_no_ve_el_dato_tardio(conn):
    cid, _ = motor.calcular(conn, 1, "GES", AHORA, conocido_hasta=datetime(2026, 3, 4, 23, 59))
    r = extractor.leer_resultado(conn, cid)
    assert 7 not in [d["prestacion_id"] for d in r["detalle"]]
    assert (r["acumulado"], r["fecha_cruce"]) == (5_100_000, "2026-02-10")
    assert r["conocido_hasta"] == "2026-03-04T23:59:00"


def test_ahora_en_el_pasado_limita_lo_conocido(conn):
    cid, _ = motor.calcular(conn, 1, "GES", datetime(2026, 3, 1))
    assert extractor.leer_resultado(conn, cid)["fecha_cruce"] == "2026-02-10"


def test_conocido_hasta_posterior_a_ahora_falla(conn):
    with pytest.raises(ValueError):
        motor.calcular(conn, 1, "GES", AHORA, conocido_hasta=datetime(2027, 1, 1))


def test_sin_copagos_calificados_no_escribe_nada(conn):
    assert motor.calcular(conn, 2, "GES", AHORA) is None               # beneficiary 2 has no GES rows
    assert conn.execute("SELECT count(*) FROM calculos_resultado").fetchone()[0] == 0


def test_version_codigo_es_un_commit():
    assert re.fullmatch(r"[0-9a-f]{40}(-dirty)?|unknown", motor.version_codigo())


# ───────────────────────────────────────────────────────────
# Versioning (S11)
# ───────────────────────────────────────────────────────────

def test_leer_resultado_vigente_reconstruye_lo_guardado(conn):
    r = _resultado_ben1(conn)
    cid = loader.guardar_resultado(conn, r, "inicial", AHORA, AHORA, "abc123")
    assert extractor.leer_resultado_vigente(conn, 1, "GES") == (cid, r)


def test_leer_resultado_vigente_sin_resultados(conn):
    assert extractor.leer_resultado_vigente(conn, 1, "GES") is None


def test_guardar_nueva_version_conserva_la_anterior(conn):
    r = _resultado_ben1(conn)
    v1 = loader.guardar_resultado(conn, r, "inicial", AHORA, AHORA, "abc123")
    v2 = loader.guardar_resultado(conn, r, "correccion", AHORA, AHORA, "abc123")
    filas = conn.execute("SELECT id, version_calculo, vigente, motivo_recalculo FROM calculos_resultado ORDER BY id").fetchall()
    assert [tuple(f) for f in filas] == [(v1, 1, 0, "inicial"), (v2, 2, 1, "correccion")]
    assert conn.execute("SELECT count(*) FROM calculos_detalle").fetchone()[0] == 12  # both detail sets kept


def test_guardar_sin_cambios_se_rechaza(conn):
    with pytest.raises(ValueError):
        loader.guardar_resultado(conn, _resultado_ben1(conn), "sin_cambios", AHORA, AHORA, "abc123")


def test_advertencias_se_guardan_y_se_reportan(conn):
    cid = loader.guardar_resultado(conn, _resultado_ben1(conn), "inicial", AHORA, AHORA, "abc123",
                                   ["prestación 3: something odd"])
    r = extractor.leer_resultado(conn, cid)
    assert r["advertencias"] == "prestación 3: something odd"
    assert "WARNING         prestación 3: something odd" in loader.reporte_texto(r)


def test_calcular_dos_veces_es_idempotente(conn):
    cid, motivo = motor.calcular(conn, 1, "GES", AHORA)
    assert motor.calcular(conn, 1, "GES", datetime(2026, 9, 28)) == (cid, "sin_cambios")
    assert conn.execute("SELECT count(*) FROM calculos_resultado").fetchone()[0] == 1


# ───────────────────────────────────────────────────────────
# cargar_regla_ges (S11, plan D10)
# ───────────────────────────────────────────────────────────

def _cargar(conn, desde, tope=125.0, codigo="GES01"):
    return loader.cargar_regla_ges(conn, codigo, desde, 29, tope, "Decreto test", "tester", AHORA)


def _versiones(conn, codigo="GES01"):
    return [(r.regla_id, r.valid_from, r.valid_to, r.tope_uf) for r in extractor.leer_reglas_ges(conn, codigo)]


def test_cargar_regla_retroactiva_parte_la_version(conn):
    rid = _cargar(conn, date(2025, 3, 1))
    assert _versiones(conn) == [
        (1, date(2025, 1, 1), date(2025, 2, 28), 122.0),
        (rid, date(2025, 3, 1), date(2025, 12, 31), 125.0),
        (2, date(2026, 1, 1), None, 130.0),
    ]
    fila = conn.execute("SELECT fuente, cargado_por, cargado_en FROM parametros_ges WHERE regla_id = ?", (rid,)).fetchone()
    assert tuple(fila) == ("Decreto test", "tester", "2026-09-27T12:00:00")


def test_cargar_regla_futura_cierra_la_abierta(conn):
    rid = _cargar(conn, date(2027, 1, 1), 140.0)
    assert _versiones(conn)[-2:] == [(2, date(2026, 1, 1), date(2026, 12, 31), 130.0), (rid, date(2027, 1, 1), None, 140.0)]


def test_cargar_regla_mismo_dia_no_cambia_nada(conn):
    antes = _versiones(conn)
    with pytest.raises(ValueError, match="already starts"):
        _cargar(conn, date(2026, 1, 1))
    assert _versiones(conn) == antes


def test_cargar_regla_que_deja_el_set_invalido_hace_rollback(conn):
    # GES02 already has a gap (inserted by hand, bypassing the loader)
    conn.executescript("""
        INSERT INTO parametros_ges (codigo_ges, valid_from, valid_to, tope_cotizaciones, tope_uf, cargado_en)
        VALUES ('GES02', '2025-01-01', '2025-03-31', 29, 100, 'x'), ('GES02', '2025-06-01', NULL, 29, 110, 'x');
    """)
    antes = _versiones(conn, "GES02")
    with pytest.raises(ValueError, match="gap"):
        _cargar(conn, date(2027, 1, 1), codigo="GES02")
    assert _versiones(conn, "GES02") == antes


# ───────────────────────────────────────────────────────────
# The four recalculation scenarios (spec §6) — why this project exists
# ───────────────────────────────────────────────────────────

def _insertar_prestacion(conn, pid, fecha, recibido_en, copago):
    """Stand-in for ingestion (S13): a late GES01 prestación for beneficiary 1."""
    with conn:
        conn.execute(
            "INSERT INTO prestaciones VALUES (?, 1, ?, ?, 'GES01', 1, 0, ?, ?)",
            (pid, fecha, recibido_en, copago * 5, copago),
        )


def _historial(conn):
    return [(f["version_calculo"], f["vigente"], f["motivo_recalculo"], f["regla_version_id"], f["ventana_inicio"])
            for f in extractor.leer_historial(conn, 1, "GES")]


def test_escenario_1_sin_cambios_no_crea_version(conn):
    motor.calcular(conn, 1, "GES", AHORA)
    _, motivo = motor.calcular(conn, 1, "GES", datetime(2026, 10, 1))
    assert motivo == "sin_cambios"
    assert _historial(conn) == [(1, 1, "inicial", 1, "2025-03-10")]


def test_escenario_2_dato_tardio_usa_la_regla_historica(conn):
    # First run on 2026-03-04: prestación 7 (received 2026-03-05) isn't known yet.
    motor.calcular(conn, 1, "GES", datetime(2026, 3, 4, 23, 59))
    cid, motivo = motor.calcular(conn, 1, "GES", AHORA)
    assert motivo == "datos_tardios"
    r = extractor.leer_resultado(conn, cid)
    # Today's rule is version 2 (130 UF); the period's rule is version 1 (122 UF). The period's wins.
    assert calculo.regla_vigente("GES01", AHORA.date(), extractor.leer_reglas_ges(conn, "GES01")).regla_id == 2
    assert (r["regla_version_id"], r["deducible_aplicado"]) == (1, 4_712_952)
    assert r["fecha_cruce"] == "2025-11-20"                       # was 2026-02-10 before prestación 7 arrived
    assert _historial(conn) == [(1, 0, "inicial", 1, "2025-03-10"), (2, 1, "datos_tardios", 1, "2025-03-10")]


def test_escenario_3_dato_anterior_desplaza_la_ventana(conn):
    motor.calcular(conn, 1, "GES", AHORA)
    _insertar_prestacion(conn, 20, "2025-02-05", "2026-09-28T09:00:00", 500_000)
    cid, motivo = motor.calcular(conn, 1, "GES", datetime(2026, 9, 28, 12, 0))
    assert motivo == "ventana_desplazada"
    r = extractor.leer_resultado(conn, cid)
    assert (r["ventana_inicio"], r["ventana_fin"]) == ("2025-02-05", "2026-02-04")
    ids = [d["prestacion_id"] for d in r["detalle"]]
    assert ids[0] == 20 and 8 not in ids                          # 8 (2026-02-10) fell off the new end
    # both windows stay in history
    assert _historial(conn) == [(1, 0, "inicial", 1, "2025-03-10"), (2, 1, "ventana_desplazada", 1, "2025-02-05")]


def test_escenario_4_cambio_normativo_retroactivo(conn):
    motor.calcular(conn, 1, "GES", AHORA)
    rid = loader.cargar_regla_ges(conn, "GES01", date(2025, 3, 1), 29, 125.0, "Decreto test", "tester", AHORA)
    cid, motivo = motor.calcular(conn, 1, "GES", datetime(2026, 9, 28))
    assert motivo == "cambio_normativo"
    r = extractor.leer_resultado(conn, cid)
    assert (r["regla_version_id"], r["deducible_aplicado"]) == (rid, 4_828_844)   # 125 × 38 630.75
    assert _historial(conn) == [(1, 0, "inicial", 1, "2025-03-10"), (2, 1, "cambio_normativo", rid, "2025-03-10")]


def test_historial_completo_de_los_cuatro_escenarios_juntos(conn):
    motor.calcular(conn, 1, "GES", datetime(2026, 3, 4, 23, 59))                       # inicial
    motor.calcular(conn, 1, "GES", datetime(2026, 3, 6))                               # datos_tardios
    motor.calcular(conn, 1, "GES", datetime(2026, 3, 7))                               # sin_cambios
    loader.cargar_regla_ges(conn, "GES01", date(2025, 3, 1), 29, 125.0, "D", "t", datetime(2026, 4, 1))
    motor.calcular(conn, 1, "GES", datetime(2026, 4, 2))                               # cambio_normativo
    _insertar_prestacion(conn, 20, "2025-02-05", "2026-05-01T09:00:00", 500_000)
    motor.calcular(conn, 1, "GES", datetime(2026, 5, 2))                               # ventana_desplazada
    assert [(v, m) for v, _, m, _, _ in _historial(conn)] == [
        (1, "inicial"), (2, "datos_tardios"), (3, "cambio_normativo"), (4, "ventana_desplazada")]
    # the window moved back to 2025-02-05, which falls under rule 1 again
    assert _historial(conn)[-1][3] == 1


# ───────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────

def test_cli_init_y_calcular(tmp_path, capsys):
    db = str(tmp_path / "data" / "motor.db")
    assert motor.main(["init", "--db", db]) == 0
    assert motor.main(["calcular", "--db", db, "--beneficiario", "1", "--tipo", "GES",
                       "--ahora", "2026-09-27T12:00"]) == 0
    salida = capsys.readouterr().out
    assert "2025-03-10 → 2026-03-09" in salida and "4,712,952" in salida and "2025-11-20" in salida


def test_cli_init_no_pisa_una_base_existente(tmp_path, capsys):
    db = str(tmp_path / "motor.db")
    motor.main(["init", "--db", db])
    assert motor.main(["init", "--db", db]) == 1
    assert "already exists" in capsys.readouterr().err


def test_cli_calcular_sin_init_falla(tmp_path, capsys):
    assert motor.main(["calcular", "--db", str(tmp_path / "no.db"), "--beneficiario", "1", "--tipo", "GES"]) == 1
    assert "init" in capsys.readouterr().err


def test_cli_segundo_calculo_sin_cambios_no_guarda(tmp_path, capsys):
    db = str(tmp_path / "motor.db")
    motor.main(["init", "--db", db])
    args = ["calcular", "--db", db, "--beneficiario", "1", "--tipo", "GES", "--ahora", "2026-09-27T12:00"]
    motor.main(args)
    assert motor.main(args) == 0
    assert "no changes since result #1" in capsys.readouterr().out


def test_cli_beneficiario_inexistente(tmp_path, capsys):
    db = str(tmp_path / "motor.db")
    motor.main(["init", "--db", db])
    assert motor.main(["calcular", "--db", db, "--beneficiario", "99", "--tipo", "GES"]) == 1
    assert "99" in capsys.readouterr().err


def test_cli_cargar_regla_y_recalcular_todos(tmp_path, capsys):
    db = str(tmp_path / "motor.db")
    motor.main(["init", "--db", db])
    motor.main(["recalcular-todos", "--db", db, "--tipo", "GES", "--ahora", "2026-09-27T12:00"])
    assert motor.main(["cargar-regla-ges", "--db", db, "--codigo", "GES01", "--desde", "2025-03-01",
                       "--tope-uf", "125", "--fuente", "Decreto test", "--ahora", "2026-09-27T13:00"]) == 0
    assert motor.main(["recalcular-todos", "--db", db, "--tipo", "GES", "--ahora", "2026-09-27T14:00"]) == 0
    salida = capsys.readouterr().out
    assert "loaded rule 3" in salida
    assert "2025-03-01 → 2025-12-31  tope 125.0 UF" in salida and "<10" not in salida
    assert "beneficiary 1: cambio_normativo" in salida and "beneficiary 2: nothing to calculate" in salida


def test_cli_cargar_regla_invalida_falla(tmp_path, capsys):
    db = str(tmp_path / "motor.db")
    motor.main(["init", "--db", db])
    assert motor.main(["cargar-regla-ges", "--db", db, "--codigo", "GES01", "--desde", "2026-01-01",
                       "--tope-uf", "125", "--fuente", "x"]) == 1
    assert "already starts" in capsys.readouterr().err


def test_cli_historial(tmp_path, capsys):
    db = str(tmp_path / "motor.db")
    motor.main(["init", "--db", db])
    motor.main(["calcular", "--db", db, "--beneficiario", "1", "--tipo", "GES", "--ahora", "2026-03-04T23:59"])
    motor.main(["calcular", "--db", db, "--beneficiario", "1", "--tipo", "GES", "--ahora", "2026-09-27T12:00"])
    capsys.readouterr()
    assert motor.main(["historial", "--db", db, "--beneficiario", "1", "--tipo", "GES"]) == 0
    salida = capsys.readouterr().out
    assert "inicial" in salida and "*  datos_tardios" in salida

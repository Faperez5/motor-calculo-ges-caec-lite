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
    with pytest.raises(sqlite3.IntegrityError):  # second current result for the same beneficiary + tipo
        loader.guardar_resultado(conn, r, "inicial", AHORA, AHORA, "abc123")
    assert conn.execute("SELECT count(*) FROM calculos_resultado").fetchone()[0] == 1
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
    cid = motor.calcular(conn, 1, "GES", AHORA, version="abc123")
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
    cid = motor.calcular(conn, 1, "GES", AHORA, conocido_hasta=datetime(2026, 3, 4, 23, 59))
    r = extractor.leer_resultado(conn, cid)
    assert 7 not in [d["prestacion_id"] for d in r["detalle"]]
    assert (r["acumulado"], r["fecha_cruce"]) == (5_100_000, "2026-02-10")
    assert r["conocido_hasta"] == "2026-03-04T23:59:00"


def test_ahora_en_el_pasado_limita_lo_conocido(conn):
    cid = motor.calcular(conn, 1, "GES", datetime(2026, 3, 1))
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


def test_cli_segundo_calculo_avisa_en_vez_de_romper(tmp_path, capsys):
    db = str(tmp_path / "motor.db")
    motor.main(["init", "--db", db])
    args = ["calcular", "--db", db, "--beneficiario", "1", "--tipo", "GES", "--ahora", "2026-09-27T12:00"]
    motor.main(args)
    assert motor.main(args) == 1
    assert "already exists" in capsys.readouterr().err


def test_cli_beneficiario_inexistente(tmp_path, capsys):
    db = str(tmp_path / "motor.db")
    motor.main(["init", "--db", db])
    assert motor.main(["calcular", "--db", db, "--beneficiario", "99", "--tipo", "GES"]) == 1
    assert "99" in capsys.readouterr().err

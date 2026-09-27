"""Tests that need a database: extractor, loader and (from S05) motor.

Each test gets a fresh in-memory SQLite with schema + seed.
"""

import sqlite3
from datetime import date, datetime

import pytest

from src import calculo, loader, extractor


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
    cid = loader.guardar_resultado(conn, r, "inicial", datetime(2026, 9, 27, 12, 0))
    cab = conn.execute("SELECT * FROM calculos_resultado WHERE id = ?", (cid,)).fetchone()
    assert (cab["version_calculo"], cab["vigente"], cab["regla_version_id"], cab["fecha_cruce"]) == (1, 1, 1, "2025-11-20")
    assert cab["calculado_en"] == "2026-09-27T12:00:00"
    suma, n = conn.execute(
        "SELECT sum(copago), count(*) FROM calculos_detalle WHERE calculo_id = ?", (cid,)
    ).fetchone()
    assert (suma, n) == (cab["acumulado"], 6)


def test_guardar_resultado_es_atomico(conn):
    r = _resultado_ben1(conn)
    loader.guardar_resultado(conn, r, "inicial", datetime(2026, 9, 27))
    with pytest.raises(sqlite3.IntegrityError):  # second current result for the same beneficiary + tipo
        loader.guardar_resultado(conn, r, "inicial", datetime(2026, 9, 28))
    assert conn.execute("SELECT count(*) FROM calculos_resultado").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM calculos_detalle").fetchone()[0] == 6

"""Unit tests for the pure core. No database, no files (except reading calculo.py's source)."""

import ast
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from src.calculo import (
    Prestacion,
    ReglaGES,
    Beneficiario,
    calcular_ges,
    chequear_datos,
    deducible_ges_uf,
    derivar_ventana,
    desglose,
    en_ventana,
    fecha_cruce,
    fecha_uf,
    filtrar_ges,
    motivo_recalculo,
    planear_nueva_version,
    regla_vigente,
    uf_a_clp,
    validar_reglas,
)

# Same two GES01 versions as sql/seed.sql.
REGLA_2025 = ReglaGES(1, "GES01", date(2025, 1, 1), date(2025, 12, 31), 29, 122.0)
REGLA_2026 = ReglaGES(2, "GES01", date(2026, 1, 1), None, 29, 130.0)
REGLAS = [REGLA_2025, REGLA_2026]


def prestacion(pid, fecha, copago, arancel=None):
    return Prestacion(
        prestacion_id=pid,
        beneficiario_id=1,
        fecha_prestacion=fecha,
        recibido_en=datetime.combine(fecha, datetime.min.time()),
        codigo_ges="GES01",
        en_red_ges=True,
        en_red_caec=False,
        monto_arancel=arancel if arancel is not None else copago * 5,
        monto_copago=copago,
    )


# Beneficiary 1's qualifying prestaciones from the seed (everything known today).
PRESTACIONES = [
    prestacion(1, date(2025, 3, 10), 1_000_000),
    prestacion(2, date(2025, 5, 20), 1_200_000),
    prestacion(5, date(2025, 8, 5), 1_500_000),
    prestacion(6, date(2025, 10, 15), 800_000),
    prestacion(7, date(2025, 11, 20), 800_000),
    prestacion(8, date(2026, 2, 10), 600_000),
]
DEDUCIBLE = 4_712_952  # 122 UF × 38 630.75


# ───────────────────────────────────────────────────────────
# regla_vigente
# ───────────────────────────────────────────────────────────

def test_frontera_exacta_del_cambio():
    assert regla_vigente("GES01", date(2025, 12, 31), REGLAS).tope_uf == 122
    assert regla_vigente("GES01", date(2026, 1, 1), REGLAS).tope_uf == 130


def test_primer_dia_de_vigencia_es_inclusivo():
    assert regla_vigente("GES01", date(2025, 1, 1), REGLAS) == REGLA_2025


def test_regla_abierta_aplica_a_futuro():
    assert regla_vigente("GES01", date(2099, 1, 1), REGLAS) == REGLA_2026


def test_antes_de_la_primera_regla_devuelve_none():
    assert regla_vigente("GES01", date(2024, 12, 31), REGLAS) is None


def test_hueco_entre_versiones_devuelve_none():
    con_hueco = [
        ReglaGES(1, "GES01", date(2025, 1, 1), date(2025, 6, 30), 29, 122.0),
        ReglaGES(2, "GES01", date(2025, 8, 1), None, 29, 130.0),
    ]
    assert regla_vigente("GES01", date(2025, 7, 15), con_hueco) is None


def test_otro_codigo_devuelve_none():
    assert regla_vigente("GES99", date(2025, 6, 1), REGLAS) is None


def test_solapamiento_falla_en_vez_de_elegir():
    solapadas = [
        ReglaGES(1, "GES01", date(2025, 1, 1), date(2025, 12, 31), 29, 122.0),
        ReglaGES(2, "GES01", date(2025, 6, 1), None, 29, 130.0),
    ]
    with pytest.raises(ValueError, match="Overlapping"):
        regla_vigente("GES01", date(2025, 7, 1), solapadas)


# ───────────────────────────────────────────────────────────
# deducible_ges_uf, uf_a_clp
# ───────────────────────────────────────────────────────────

def test_deducible_ges_bajo_el_tope():
    assert deducible_ges_uf(3.5, REGLA_2025) == pytest.approx(101.5)  # 29 × 3.5


def test_deducible_ges_topado():
    assert deducible_ges_uf(5.0, REGLA_2025) == 122  # 29 × 5.0 = 145 > 122
    assert deducible_ges_uf(5.0, REGLA_2026) == 130


def test_deducible_ges_rechaza_cotizacion_no_positiva():
    with pytest.raises(ValueError):
        deducible_ges_uf(0, REGLA_2025)


def test_uf_a_clp_redondea_a_pesos():
    assert uf_a_clp(122, 38_630.75) == DEDUCIBLE
    assert isinstance(uf_a_clp(122, 38_630.75), int)


def test_uf_a_clp_redondeo_bancario():
    # 0.5 goes to the even neighbour, both ways (D4).
    assert uf_a_clp(1, 2.5) == 2
    assert uf_a_clp(1, 3.5) == 4


# ───────────────────────────────────────────────────────────
# desglose — invariants (spec §8.2)
# ───────────────────────────────────────────────────────────

def test_desglose_suma_al_copago():
    for f in desglose(PRESTACIONES, DEDUCIBLE):
        assert f.paga_beneficiario + f.cubre_seguro == f.copago


def test_nunca_negativo():
    for f in desglose(PRESTACIONES, DEDUCIBLE):
        assert f.paga_beneficiario >= 0 and f.cubre_seguro >= 0


def test_beneficiario_paga_exactamente_el_deducible():
    filas = desglose(PRESTACIONES, DEDUCIBLE)
    assert sum(f.paga_beneficiario for f in filas) == DEDUCIBLE


def test_beneficiario_paga_todo_si_no_alcanza_el_deducible():
    filas = desglose(PRESTACIONES[:2], DEDUCIBLE)
    assert all(f.cubre_seguro == 0 for f in filas)


def test_fila_que_cruza_se_divide():
    filas = desglose([prestacion(1, date(2025, 1, 1), 1_000_000),
                      prestacion(2, date(2025, 2, 1), 1_000_000)], 1_500_000)
    assert (filas[1].paga_beneficiario, filas[1].cubre_seguro) == (500_000, 500_000)


def test_deducible_cero_todo_lo_cubre_el_seguro():
    assert all(f.paga_beneficiario == 0 for f in desglose(PRESTACIONES, 0))


def test_desglose_no_depende_del_orden_de_entrada():
    assert desglose(list(reversed(PRESTACIONES)), DEDUCIBLE) == desglose(PRESTACIONES, DEDUCIBLE)


def test_desglose_idempotente():
    assert desglose(PRESTACIONES, DEDUCIBLE) == desglose(PRESTACIONES, DEDUCIBLE)


def test_desglose_acumulado_es_corriente():
    filas = desglose(PRESTACIONES, DEDUCIBLE)
    assert [f.acumulado for f in filas] == [
        1_000_000, 2_200_000, 3_700_000, 4_500_000, 5_300_000, 5_900_000,
    ]


def test_fecha_cruce_es_la_fila_que_alcanza_el_deducible():
    assert fecha_cruce(desglose(PRESTACIONES, DEDUCIBLE), DEDUCIBLE) == date(2025, 11, 20)


def test_fecha_cruce_se_mueve_sin_el_dato_tardio():
    sin_tardio = [p for p in PRESTACIONES if p.prestacion_id != 7]
    assert fecha_cruce(desglose(sin_tardio, DEDUCIBLE), DEDUCIBLE) == date(2026, 2, 10)


def test_fecha_cruce_none_si_no_se_alcanza():
    assert fecha_cruce(desglose(PRESTACIONES[:2], DEDUCIBLE), DEDUCIBLE) is None


def test_fecha_cruce_exacta_en_el_umbral():
    filas = desglose([prestacion(1, date(2025, 1, 1), 1_000)], 1_000)
    assert fecha_cruce(filas, 1_000) == date(2025, 1, 1)


# ───────────────────────────────────────────────────────────
# Filter, window, UF date
# ───────────────────────────────────────────────────────────

def test_filtrar_ges_excluye_fuera_de_red_y_otros_codigos():
    base = prestacion(1, date(2025, 3, 10), 1_000)
    ps = [base, replace(base, prestacion_id=2, en_red_ges=False),
          replace(base, prestacion_id=3, codigo_ges=None), replace(base, prestacion_id=4, codigo_ges="GES02")]
    assert [p.prestacion_id for p in filtrar_ges(ps, "GES01")] == [1]


def test_derivar_ventana_desde_el_primer_copago():
    assert derivar_ventana(PRESTACIONES) == (date(2025, 3, 10), date(2026, 3, 9))


def test_derivar_ventana_fin_de_mes():
    assert derivar_ventana([prestacion(1, date(2025, 1, 31), 1)]) == (date(2025, 1, 31), date(2026, 1, 30))


def test_derivar_ventana_termina_en_29_de_febrero_si_corresponde():
    assert derivar_ventana([prestacion(1, date(2023, 3, 1), 1)])[1] == date(2024, 2, 29)


def test_derivar_ventana_que_parte_un_29_de_febrero_termina_el_28():
    assert derivar_ventana([prestacion(1, date(2024, 2, 29), 1)]) == (date(2024, 2, 29), date(2025, 2, 28))


def test_ventana_se_desplaza_con_dato_anterior():
    v1 = derivar_ventana(PRESTACIONES)
    v2 = derivar_ventana(PRESTACIONES + [prestacion(20, date(2025, 2, 5), 500_000)])
    assert v2[0] < v1[0]
    assert v2 == (date(2025, 2, 5), date(2026, 2, 4))


def test_ventana_desplazada_saca_prestaciones_que_estaban_dentro():
    tardio = prestacion(20, date(2025, 2, 5), 500_000)
    antes = {p.prestacion_id for p in en_ventana(PRESTACIONES, derivar_ventana(PRESTACIONES))}
    todas = PRESTACIONES + [tardio]
    despues = {p.prestacion_id for p in en_ventana(todas, derivar_ventana(todas))}
    assert 8 in antes and 8 not in despues      # 2026-02-10 falls off the new end (2026-02-04)
    assert 20 in despues                        # the late arrival itself is now the start


def test_ventana_desplazada_mueve_el_cruce():
    uf = {**UF, date(2025, 1, 31): 38_515.20}
    antes = calcular_ges(BEN_1, PRESTACIONES, REGLAS, "GES01", UF)
    despues = calcular_ges(BEN_1, PRESTACIONES + [prestacion(20, date(2025, 2, 5), 500_000)], REGLAS, "GES01", uf)
    assert antes.fecha_cruce == date(2025, 11, 20)
    # UF(2025-01-31) → deductible 4 698 854; 0.5+1.0+1.2+1.5+0.8 = 5.0M crosses on 2025-10-15
    assert (despues.deducible_aplicado, despues.fecha_cruce) == (4_698_854, date(2025, 10, 15))


def test_derivar_ventana_vacia_es_none():
    assert derivar_ventana([]) is None


def test_en_ventana_incluye_ambos_extremos():
    ps = [prestacion(1, date(2025, 3, 10), 1), prestacion(2, date(2026, 3, 9), 1), prestacion(3, date(2026, 3, 10), 1)]
    assert [p.prestacion_id for p in en_ventana(ps, (date(2025, 3, 10), date(2026, 3, 9)))] == [1, 2]


def test_fecha_uf_es_fin_del_mes_anterior():
    assert fecha_uf(date(2025, 3, 10)) == date(2025, 2, 28)
    assert fecha_uf(date(2026, 3, 15)) == date(2026, 2, 28)
    assert fecha_uf(date(2024, 3, 1)) == date(2024, 2, 29)    # leap year
    assert fecha_uf(date(2025, 1, 31)) == date(2024, 12, 31)  # year boundary


# ───────────────────────────────────────────────────────────
# calcular_ges
# ───────────────────────────────────────────────────────────

BEN_1 = Beneficiario(1, 5.0, None)
UF = {date(2025, 2, 28): 38_630.75}


def test_calcular_ges_con_los_datos_del_seed():
    r = calcular_ges(BEN_1, PRESTACIONES, REGLAS, "GES01", UF)
    assert (r.regla_version_id, r.deducible_aplicado, r.acumulado, r.fecha_cruce) == (
        1, DEDUCIBLE, 5_900_000, date(2025, 11, 20))
    assert (r.ventana_inicio, r.ventana_fin) == (date(2025, 3, 10), date(2026, 3, 9))


def test_calcular_ges_usa_la_regla_del_inicio_de_ventana():
    # prestación 8 (2026-02-10) falls under rule 2's dates, but the window started under rule 1 (D1)
    assert calcular_ges(BEN_1, PRESTACIONES, REGLAS, "GES01", UF).regla_version_id == 1


def test_calcular_ges_sin_calificadas_devuelve_none():
    assert calcular_ges(BEN_1, [], REGLAS, "GES01", UF) is None


def test_calcular_ges_sin_regla_falla():
    with pytest.raises(LookupError, match="rule"):
        calcular_ges(BEN_1, PRESTACIONES, [REGLA_2026], "GES01", UF)


def test_calcular_ges_sin_uf_falla():
    with pytest.raises(LookupError, match="UF"):
        calcular_ges(BEN_1, PRESTACIONES, REGLAS, "GES01", {})


def test_calcular_ges_rechaza_prestaciones_de_otro_beneficiario():
    with pytest.raises(ValueError):
        calcular_ges(BEN_1, PRESTACIONES + [replace(PRESTACIONES[0], beneficiario_id=2)], REGLAS, "GES01", UF)


def test_calcular_ges_es_idempotente():
    assert calcular_ges(BEN_1, PRESTACIONES, REGLAS, "GES01", UF) == calcular_ges(BEN_1, PRESTACIONES, REGLAS, "GES01", UF)


# ───────────────────────────────────────────────────────────
# motivo_recalculo — one test per reason
# ───────────────────────────────────────────────────────────

BASE = calcular_ges(BEN_1, PRESTACIONES, REGLAS, "GES01", UF)


def test_motivo_inicial():
    assert motivo_recalculo(None, BASE) == "inicial"


def test_motivo_sin_cambios():
    assert motivo_recalculo(BASE, calcular_ges(BEN_1, PRESTACIONES, REGLAS, "GES01", UF)) == "sin_cambios"


def test_motivo_ventana_desplazada():
    uf = {**UF, date(2025, 1, 31): 38_515.20}
    nuevo = calcular_ges(BEN_1, PRESTACIONES + [prestacion(20, date(2025, 2, 5), 1)], REGLAS, "GES01", uf)
    assert motivo_recalculo(BASE, nuevo) == "ventana_desplazada"


def test_motivo_cambio_normativo():
    reglas = [replace(REGLA_2025, valid_to=date(2025, 2, 28)),
              ReglaGES(3, "GES01", date(2025, 3, 1), date(2025, 12, 31), 29, 125.0), REGLA_2026]
    assert motivo_recalculo(BASE, calcular_ges(BEN_1, PRESTACIONES, reglas, "GES01", UF)) == "cambio_normativo"


def test_motivo_correccion():
    nuevo = calcular_ges(BEN_1, PRESTACIONES, REGLAS, "GES01", {date(2025, 2, 28): 38_700.0})
    assert motivo_recalculo(BASE, nuevo) == "correccion"


def test_motivo_datos_tardios():
    sin_tardio = calcular_ges(BEN_1, [p for p in PRESTACIONES if p.prestacion_id != 7], REGLAS, "GES01", UF)
    assert motivo_recalculo(sin_tardio, BASE) == "datos_tardios"


# ───────────────────────────────────────────────────────────
# validar_reglas (spec §8.1)
# ───────────────────────────────────────────────────────────

def test_reglas_del_seed_son_validas():
    assert validar_reglas(REGLAS) == []


def test_validar_detecta_solapamiento():
    malas = [REGLA_2025, ReglaGES(2, "GES01", date(2025, 12, 1), None, 29, 130.0)]
    assert any("overlap" in p for p in validar_reglas(malas))


def test_validar_detecta_hueco():
    malas = [REGLA_2025, ReglaGES(2, "GES01", date(2026, 2, 1), None, 29, 130.0)]
    assert any("gap" in p for p in validar_reglas(malas))


def test_validar_detecta_dos_versiones_abiertas():
    malas = [replace(REGLA_2025, valid_to=None), REGLA_2026]
    problemas = validar_reglas(malas)
    assert any("2 open" in p for p in problemas) and any("overlap" in p for p in problemas)


def test_validar_detecta_ninguna_version_abierta():
    assert any("0 open" in p for p in validar_reglas([REGLA_2025]))


def test_validar_detecta_fechas_invertidas():
    malas = [ReglaGES(1, "GES01", date(2025, 6, 1), date(2025, 1, 1), 29, 122.0), REGLA_2026]
    assert any("ends before it starts" in p for p in validar_reglas(malas))


def test_version_de_un_dia_es_valida():
    reglas = [REGLA_2025, ReglaGES(2, "GES01", date(2026, 1, 1), date(2026, 1, 1), 29, 130.0),
              ReglaGES(3, "GES01", date(2026, 1, 2), None, 29, 135.0)]
    assert validar_reglas(reglas) == []


# ───────────────────────────────────────────────────────────
# planear_nueva_version
# ───────────────────────────────────────────────────────────

def test_nueva_version_futura_cierra_la_abierta():
    assert planear_nueva_version(REGLAS, "GES01", date(2027, 1, 1)) == ([(2, date(2026, 12, 31))], None)


def test_nueva_version_retroactiva_parte_la_que_la_contiene():
    assert planear_nueva_version(REGLAS, "GES01", date(2025, 3, 1)) == (
        [(1, date(2025, 2, 28))], date(2025, 12, 31))


def test_nueva_version_anterior_a_todas_llega_hasta_la_primera():
    assert planear_nueva_version(REGLAS, "GES01", date(2024, 1, 1)) == ([], date(2024, 12, 31))


def test_primera_version_de_un_codigo_queda_abierta():
    assert planear_nueva_version(REGLAS, "GES02", date(2025, 1, 1)) == ([], None)


def test_nueva_version_el_mismo_dia_que_otra_falla():
    with pytest.raises(ValueError, match="already starts"):
        planear_nueva_version(REGLAS, "GES01", date(2026, 1, 1))


@pytest.mark.parametrize("desde", [date(2024, 1, 1), date(2025, 3, 1), date(2026, 1, 2), date(2027, 1, 1)])
def test_aplicar_el_plan_deja_un_set_valido(desde):
    cierres, hasta = planear_nueva_version(REGLAS, "GES01", desde)
    nuevo_to = dict(cierres)
    reglas = [replace(r, valid_to=nuevo_to.get(r.regla_id, r.valid_to)) for r in REGLAS]
    reglas.append(ReglaGES(99, "GES01", desde, hasta, 29, 140.0))
    assert validar_reglas(reglas) == []


# ───────────────────────────────────────────────────────────
# chequear_datos (spec §8.3)
# ───────────────────────────────────────────────────────────

def test_datos_del_seed_estan_limpios():
    assert chequear_datos(PRESTACIONES, REGLAS) == []


def test_chequear_copago_mayor_que_arancel():
    avisos = chequear_datos([prestacion(1, date(2025, 3, 1), 1_000, arancel=500)], REGLAS)
    assert any("> arancel" in a for a in avisos)


def test_chequear_copago_ges_sobre_20_por_ciento():
    avisos = chequear_datos([prestacion(1, date(2025, 3, 1), 300, arancel=1_000)], REGLAS)
    assert any("20%" in a for a in avisos)


def test_chequear_prestacion_sin_regla_vigente():
    avisos = chequear_datos([prestacion(1, date(2024, 6, 1), 100)], REGLAS)
    assert any("no GES01 rule in force" in a for a in avisos)


def test_chequear_codigo_huerfano():
    avisos = chequear_datos([replace(prestacion(1, date(2025, 6, 1), 100), codigo_ges="GES77")], REGLAS)
    assert any("GES77 has no rules" in a for a in avisos)


def test_chequear_ignora_reglas_ges_en_no_ges():
    assert chequear_datos([replace(prestacion(1, date(2024, 6, 1), 300, arancel=1_000), codigo_ges=None)], REGLAS) == []


# ───────────────────────────────────────────────────────────
# Architecture guard: calculo.py stays pure
# ───────────────────────────────────────────────────────────

PROHIBIDOS_IMPORT = {"sqlite3", "os", "io", "pathlib", "openpyxl", "extractor", "loader", "motor"}
PROHIBIDAS_LLAMADAS = {"now", "today", "utcnow", "open"}


def test_calculo_no_hace_io():
    fuente = Path(__file__).parent.parent / "src" / "calculo.py"
    arbol = ast.parse(fuente.read_text(encoding="utf-8"))
    problemas = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres = [a.name for a in nodo.names]
        elif isinstance(nodo, ast.ImportFrom):
            nombres = [nodo.module or ""] + [a.name for a in nodo.names]
        elif isinstance(nodo, ast.Call):
            f = nodo.func
            nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if nombre in PROHIBIDAS_LLAMADAS:
                problemas.append(f"line {nodo.lineno}: calls {nombre}()")
            continue
        else:
            continue
        for n in nombres:
            if set(n.split(".")) & PROHIBIDOS_IMPORT:
                problemas.append(f"line {nodo.lineno}: imports {n}")
    assert not problemas, "calculo.py must stay pure:\n" + "\n".join(problemas)

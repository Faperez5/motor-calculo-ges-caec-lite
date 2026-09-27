"""LOAD: every write to the database, and later the Excel/Word reports.

Results are versioned, never overwritten. Facts (prestaciones) are
append-only. Rules only change through cargar_regla (S11, plan D10).

Phase 1: every result is version 1 and current. Versioning, idempotence and
motivo detection arrive in S11.
"""

from datetime import datetime

from src.calculo import Resultado


def guardar_resultado(conn, resultado: Resultado, motivo: str, calculado_en: datetime,
                      conocido_hasta: datetime, version_codigo: str) -> int:
    """Insert the header and its detail rows in one transaction. Returns the new id.

    `conocido_hasta` and `version_codigo` are run metadata: what the run knew,
    and which code produced the number.

    If anything fails (e.g. a current result already exists for this
    beneficiary and tipo), nothing is written.
    """
    with conn:
        cur = conn.execute(
            """
            INSERT INTO calculos_resultado
                (beneficiario_id, tipo, version_calculo, regla_version_id,
                 ventana_inicio, ventana_fin, acumulado, deducible_aplicado,
                 fecha_cruce, calculado_en, conocido_hasta, version_codigo,
                 motivo_recalculo, vigente)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                resultado.beneficiario_id,
                resultado.tipo,
                resultado.regla_version_id,
                resultado.ventana_inicio.isoformat(),
                resultado.ventana_fin.isoformat(),
                resultado.acumulado,
                resultado.deducible_aplicado,
                resultado.fecha_cruce.isoformat() if resultado.fecha_cruce else None,
                calculado_en.isoformat(timespec="seconds"),
                conocido_hasta.isoformat(timespec="seconds"),
                version_codigo,
                motivo,
            ),
        )
        calculo_id = cur.lastrowid
        conn.executemany(
            """
            INSERT INTO calculos_detalle
                (calculo_id, prestacion_id, copago, paga_beneficiario, cubre_seguro, acumulado_hasta_aqui)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (calculo_id, f.prestacion_id, f.copago, f.paga_beneficiario, f.cubre_seguro, f.acumulado)
                for f in resultado.filas
            ],
        )
    return calculo_id


# ───────────────────────────────────────────────────────────
# Reports: built from a saved result (extractor.leer_resultado),
# never from in-memory values, so they can be regenerated later.
# ───────────────────────────────────────────────────────────

def reporte_texto(r: dict) -> str:
    """Audit trace of a saved result, for the terminal."""
    estado = "current" if r["vigente"] else "superseded"
    lineas = [
        f"Result #{r['id']} — beneficiary {r['beneficiario_id']}, {r['tipo']}, "
        f"version {r['version_calculo']} ({estado}), motivo: {r['motivo_recalculo']}",
        f"  rule version    {r['regla_version_id']}",
        f"  window          {r['ventana_inicio']} → {r['ventana_fin']}",
        f"  deductible      {r['deducible_aplicado']:>12,} CLP",
        f"  accumulated     {r['acumulado']:>12,} CLP",
        f"  crossed on      {r['fecha_cruce'] or 'not reached'}",
        f"  calculated at   {r['calculado_en']}   (data known up to {r['conocido_hasta']})",
        f"  code version    {r['version_codigo']}",
        "",
        f"  {'id':>3}  {'fecha':<10}  {'copago':>11}  {'beneficiary':>11}  {'insurer':>11}  {'accumulated':>11}",
    ]
    for d in r["detalle"]:
        lineas.append(
            f"  {d['prestacion_id']:>3}  {d['fecha_prestacion']:<10}  {d['copago']:>11,}  "
            f"{d['paga_beneficiario']:>11,}  {d['cubre_seguro']:>11,}  {d['acumulado_hasta_aqui']:>11,}"
        )
    return "\n".join(lineas)

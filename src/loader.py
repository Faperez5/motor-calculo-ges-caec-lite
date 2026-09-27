"""LOAD: every write to the database, and later the Excel/Word reports.

Results are versioned: a new version marks the previous one vigente = 0 and
never overwrites it. Facts (prestaciones) are append-only. Rules only change
through cargar_regla_ges (plan D10).
"""

from datetime import date, datetime

from src import calculo, extractor
from src.calculo import Resultado


def iniciar_ejecucion(conn, comando: str, usuario: str, version_codigo: str, iniciado_en: datetime) -> int:
    """Log a command as started, committed at once so a crash still leaves a trace."""
    with conn:
        cur = conn.execute(
            "INSERT INTO ejecuciones (comando, iniciado_en, usuario, version_codigo, estado) "
            "VALUES (?, ?, ?, ?, 'en_curso')",
            (comando, iniciado_en.isoformat(timespec="seconds"), usuario, version_codigo),
        )
    return cur.lastrowid


def terminar_ejecucion(conn, ejecucion_id: int, estado: str, mensaje: str | None, terminado_en: datetime) -> None:
    with conn:
        conn.execute(
            "UPDATE ejecuciones SET estado = ?, mensaje = ?, terminado_en = ? WHERE id = ?",
            (estado, mensaje, terminado_en.isoformat(timespec="seconds"), ejecucion_id),
        )


def guardar_resultado(conn, resultado: Resultado, motivo: str, calculado_en: datetime,
                      conocido_hasta: datetime, version_codigo: str, advertencias=(),
                      ejecucion_id: int | None = None) -> int:
    """Save `resultado` as the new current version. Returns its id.

    One transaction: the previous current version (if any) is marked vigente = 0
    and the new one gets version_calculo = previous + 1. If anything fails,
    nothing changes, including the previous version's vigente flag.
    `sin_cambios` is refused: an unchanged result must not create a version.
    """
    if motivo == "sin_cambios":
        raise ValueError("sin_cambios: nothing to save")
    clave = (resultado.beneficiario_id, resultado.tipo)
    with conn:
        ultima = conn.execute(
            "SELECT max(version_calculo) FROM calculos_resultado WHERE beneficiario_id = ? AND tipo = ?", clave
        ).fetchone()[0]
        conn.execute(
            "UPDATE calculos_resultado SET vigente = 0 WHERE beneficiario_id = ? AND tipo = ? AND vigente = 1", clave
        )
        cur = conn.execute(
            """
            INSERT INTO calculos_resultado
                (beneficiario_id, tipo, version_calculo, regla_version_id,
                 ventana_inicio, ventana_fin, acumulado, deducible_aplicado,
                 fecha_cruce, calculado_en, conocido_hasta, version_codigo,
                 advertencias, ejecucion_id, motivo_recalculo, vigente)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                resultado.beneficiario_id,
                resultado.tipo,
                (ultima or 0) + 1,
                resultado.regla_version_id,
                resultado.ventana_inicio.isoformat(),
                resultado.ventana_fin.isoformat(),
                resultado.acumulado,
                resultado.deducible_aplicado,
                resultado.fecha_cruce.isoformat() if resultado.fecha_cruce else None,
                calculado_en.isoformat(timespec="seconds"),
                conocido_hasta.isoformat(timespec="seconds"),
                version_codigo,
                "\n".join(advertencias) or None,
                ejecucion_id,
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


def cargar_regla_ges(conn, codigo_ges: str, valid_from: date, tope_cotizaciones: int, tope_uf: float,
                     fuente: str | None, cargado_por: str, cargado_en: datetime,
                     ejecucion_id: int | None = None) -> int:
    """Load a new GES rule version (plan D10). Returns its regla_id.

    One transaction: close the version that contains `valid_from`, insert the
    new one, re-read the whole set and validate it (§8.1). Any problem rolls
    everything back.
    """
    with conn:
        cierres, valid_to = calculo.planear_nueva_version(
            extractor.leer_reglas_ges(conn, codigo_ges), codigo_ges, valid_from)
        for regla_id, hasta in cierres:
            conn.execute("UPDATE parametros_ges SET valid_to = ? WHERE regla_id = ?", (hasta.isoformat(), regla_id))
        cur = conn.execute(
            """
            INSERT INTO parametros_ges
                (codigo_ges, valid_from, valid_to, tope_cotizaciones, tope_uf, fuente, cargado_por, cargado_en,
                 ejecucion_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (codigo_ges, valid_from.isoformat(), valid_to.isoformat() if valid_to else None,
             tope_cotizaciones, tope_uf, fuente, cargado_por, cargado_en.isoformat(timespec="seconds"),
             ejecucion_id),
        )
        problemas = calculo.validar_reglas(extractor.leer_reglas_ges(conn, codigo_ges))
        if problemas:
            raise ValueError("Rule set would be invalid: " + "; ".join(problemas))
    return cur.lastrowid


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
        *[f"  WARNING         {a}" for a in (r["advertencias"] or "").splitlines()],
        "",
        f"  {'id':>3}  {'fecha':<10}  {'copago':>11}  {'beneficiary':>11}  {'insurer':>11}  {'accumulated':>11}",
    ]
    for d in r["detalle"]:
        lineas.append(
            f"  {d['prestacion_id']:>3}  {d['fecha_prestacion']:<10}  {d['copago']:>11,}  "
            f"{d['paga_beneficiario']:>11,}  {d['cubre_seguro']:>11,}  {d['acumulado_hasta_aqui']:>11,}"
        )
    return "\n".join(lineas)


def reporte_historial(filas: list[dict]) -> str:
    """One line per saved version, to see how and why a result changed."""
    if not filas:
        return "no saved results"
    lineas = [f"  {'ver':>3}  {'':1}  {'motivo':<18}  {'rule':>4}  {'window':<23}  {'deductible':>11}  "
              f"{'crossed on':<10}  calculated at"]
    for f in filas:
        lineas.append(
            f"  {f['version_calculo']:>3}  {'*' if f['vigente'] else ' '}  {f['motivo_recalculo']:<18}  "
            f"{f['regla_version_id']:>4}  {f['ventana_inicio']} → {f['ventana_fin']}  {f['deducible_aplicado']:>11,}  "
            f"{f['fecha_cruce'] or '-':<10}  {f['calculado_en']}"
        )
    return "\n".join(lineas) + "\n  (* = current)"


def reporte_ejecuciones(filas: list[dict]) -> str:
    """One line per logged command, newest first."""
    if not filas:
        return "no runs logged"
    lineas = [f"  {'id':>4}  {'started':<19}  {'user':<10}  {'state':<8}  {'code':<8}  command → outcome"]
    for f in filas:
        lineas.append(
            f"  {f['id']:>4}  {f['iniciado_en']:<19}  {f['usuario'][:10]:<10}  {f['estado']:<8}  "
            f"{f['version_codigo'][:7] + ('*' if f['version_codigo'].endswith('-dirty') else ''):<8}  "
            f"{f['comando']}" + (f"  →  {f['mensaje']}" if f["mensaje"] else "")
        )
    return "\n".join(lineas) + "\n  (code* = uncommitted changes)"

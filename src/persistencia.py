"""Write side of the engine: stores results. Never updates facts or rules.

Phase 1: every result is version 1 and current. Versioning, idempotence and
motivo detection arrive in S11.
"""

from datetime import datetime

from src.calculo import Resultado


def guardar_resultado(conn, resultado: Resultado, motivo: str, calculado_en: datetime) -> int:
    """Insert the header and its detail rows in one transaction. Returns the new id.

    If anything fails (e.g. a current result already exists for this
    beneficiary and tipo), nothing is written.
    """
    with conn:
        cur = conn.execute(
            """
            INSERT INTO calculos_resultado
                (beneficiario_id, tipo, version_calculo, regla_version_id,
                 ventana_inicio, ventana_fin, acumulado, deducible_aplicado,
                 fecha_cruce, calculado_en, motivo_recalculo, vigente)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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

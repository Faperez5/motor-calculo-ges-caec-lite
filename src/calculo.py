"""Pure core of the engine: rules + prestaciones in, results out.

Nothing in this module does I/O. No sqlite3, no open(), no datetime.now():
every input, including the calculation date, arrives as an argument
(spec §2.3). tests/test_calculo.py enforces this with an AST check.

Money: CLP amounts are int pesos, UF amounts are float. The only rounding
happens in uf_a_clp (plan D4).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta


# ───────────────────────────────────────────────────────────
# Data
# ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ReglaGES:
    regla_id: int
    codigo_ges: str
    valid_from: date
    valid_to: date | None           # None = in force; inclusive otherwise
    tope_cotizaciones: int          # deductible = this × cotización pactada...
    tope_uf: float                  # ...capped at this many UF


@dataclass(frozen=True)
class ReglaCAEC:
    regla_id: int
    valid_from: date
    valid_to: date | None
    factor_base: int
    factor_multiple: int
    piso_uf: float
    techo_base_uf: float
    techo_multiple_uf: float


@dataclass(frozen=True)
class Prestacion:
    prestacion_id: int
    beneficiario_id: int
    fecha_prestacion: date          # valid time
    recibido_en: datetime           # transaction time
    codigo_ges: str | None
    en_red_ges: bool
    en_red_caec: bool
    monto_arancel: int
    monto_copago: int


@dataclass(frozen=True)
class Beneficiario:
    beneficiario_id: int
    cotizacion_pactada_uf: float
    fecha_derivacion_caec: date | None   # None = not referred


@dataclass(frozen=True)
class FilaDesglose:
    prestacion_id: int
    fecha: date
    copago: int
    paga_beneficiario: int
    cubre_seguro: int
    acumulado: int


@dataclass(frozen=True)
class Resultado:
    """One calculation, with everything needed to explain it (spec §1, point 2)."""
    beneficiario_id: int
    tipo: str                       # 'GES' | 'CAEC'
    regla_version_id: int           # lineage
    ventana_inicio: date
    ventana_fin: date
    acumulado: int                  # CLP
    deducible_aplicado: int         # CLP threshold used for the split
    fecha_cruce: date | None        # None = deductible not reached
    filas: tuple[FilaDesglose, ...]


# ───────────────────────────────────────────────────────────
# Rules: effective-dated lookup (spec §7.1, in Python)
# ───────────────────────────────────────────────────────────

def _vigente_en(regla, fecha: date) -> bool:
    return regla.valid_from <= fecha and (regla.valid_to is None or fecha <= regla.valid_to)


def regla_vigente(codigo_ges: str, fecha: date, reglas) -> ReglaGES | None:
    """The GES rule for `codigo_ges` in force on `fecha`.

    Returns None when `fecha` falls in a gap, so the caller has to handle it
    (the Python version of the LEFT JOIN in §7.1). Raises if two versions
    overlap: that is a broken rule set, never something to pick from.
    """
    candidatas = [r for r in reglas if r.codigo_ges == codigo_ges and _vigente_en(r, fecha)]
    if len(candidatas) > 1:
        ids = sorted(r.regla_id for r in candidatas)
        raise ValueError(f"Overlapping rules for {codigo_ges} on {fecha}: {ids}")
    return candidatas[0] if candidatas else None


# ───────────────────────────────────────────────────────────
# Deductibles and conversion
# ───────────────────────────────────────────────────────────

def deducible_ges_uf(cotizacion_pactada_uf: float, regla: ReglaGES) -> float:
    """GES deductible in UF: N cotizaciones, capped at the rule's tope (§5.1)."""
    if cotizacion_pactada_uf <= 0:
        raise ValueError("cotizacion_pactada_uf must be > 0")
    return min(regla.tope_cotizaciones * cotizacion_pactada_uf, regla.tope_uf)


def uf_a_clp(monto_uf: float, valor_uf: float) -> int:
    """UF → whole CLP pesos. round() is half-to-even; this is the only rounding (D4)."""
    if valor_uf <= 0:
        raise ValueError("valor_uf must be > 0")
    return round(monto_uf * valor_uf)


# ───────────────────────────────────────────────────────────
# Split at the threshold (spec §7.4)
# ───────────────────────────────────────────────────────────

def desglose(prestaciones, deducible_clp: int) -> tuple[FilaDesglose, ...]:
    """Split each copago between beneficiary and insurer.

    Sorts by (fecha_prestacion, prestacion_id) itself, so the result doesn't
    depend on input order. Before the threshold the beneficiary pays the whole
    copago; after it, `restante` is 0 and the insurer covers it. No special
    case for the row that crosses.
    """
    if deducible_clp < 0:
        raise ValueError("deducible_clp must be >= 0")

    acumulado = 0
    filas = []
    for p in sorted(prestaciones, key=lambda p: (p.fecha_prestacion, p.prestacion_id)):
        restante = max(0, deducible_clp - acumulado)
        paga_beneficiario = min(p.monto_copago, restante)
        acumulado += p.monto_copago
        filas.append(FilaDesglose(
            prestacion_id=p.prestacion_id,
            fecha=p.fecha_prestacion,
            copago=p.monto_copago,
            paga_beneficiario=paga_beneficiario,
            cubre_seguro=p.monto_copago - paga_beneficiario,
            acumulado=acumulado,
        ))
    return tuple(filas)


def fecha_cruce(filas, deducible_clp: int) -> date | None:
    """Date of the first prestación whose running total reaches the deductible."""
    return next((f.fecha for f in filas if f.acumulado >= deducible_clp), None)


# ───────────────────────────────────────────────────────────
# Qualifying prestaciones and the derived window (spec §5.3, §7.5)
# ───────────────────────────────────────────────────────────

def filtrar_ges(prestaciones, codigo_ges: str) -> list[Prestacion]:
    """Copagos that count for a GES problem: that code, inside the GES network (§5.1)."""
    return [p for p in prestaciones if p.codigo_ges == codigo_ges and p.en_red_ges]


def derivar_ventana(prestaciones) -> tuple[date, date] | None:
    """12 months from the first known qualifying copago. Moves if an earlier one shows up.

    Ends the day before the anniversary. A window starting Feb 29 has its
    anniversary on Mar 1, so it ends Feb 28 (plan D11).
    """
    if not prestaciones:
        return None
    inicio = min(p.fecha_prestacion for p in prestaciones)
    try:
        aniversario = inicio.replace(year=inicio.year + 1)
    except ValueError:  # Feb 29 → no such day next year
        aniversario = date(inicio.year + 1, 3, 1)
    return inicio, aniversario - timedelta(days=1)


def en_ventana(prestaciones, ventana: tuple[date, date]) -> list[Prestacion]:
    inicio, fin = ventana
    return [p for p in prestaciones if inicio <= p.fecha_prestacion <= fin]


def fecha_uf(ventana_inicio: date) -> date:
    """UF date for a window: last day of the month before it starts (plan D3)."""
    return ventana_inicio.replace(day=1) - timedelta(days=1)


# ───────────────────────────────────────────────────────────
# Whole calculation
# ───────────────────────────────────────────────────────────

def calcular_ges(beneficiario: Beneficiario, prestaciones, reglas, codigo_ges: str,
                 valores_uf) -> Resultado | None:
    """filter → window → rule on ventana_inicio (D1) → deductible → split.

    `valores_uf` maps date → CLP. Returns None when there is nothing to
    calculate (no qualifying copagos). A missing rule or UF value is an error,
    never a silent skip.
    """
    ajenas = {p.beneficiario_id for p in prestaciones} - {beneficiario.beneficiario_id}
    if ajenas:
        raise ValueError(f"Prestaciones of other beneficiaries passed in: {sorted(ajenas)}")

    calificadas = filtrar_ges(prestaciones, codigo_ges)
    ventana = derivar_ventana(calificadas)
    if ventana is None:
        return None
    inicio, fin = ventana

    regla = regla_vigente(codigo_ges, inicio, reglas)
    if regla is None:
        raise LookupError(f"No {codigo_ges} rule in force on {inicio}")
    dia_uf = fecha_uf(inicio)
    if dia_uf not in valores_uf:
        raise LookupError(f"No UF value for {dia_uf}")

    deducible = uf_a_clp(deducible_ges_uf(beneficiario.cotizacion_pactada_uf, regla), valores_uf[dia_uf])
    filas = desglose(en_ventana(calificadas, ventana), deducible)
    return Resultado(
        beneficiario_id=beneficiario.beneficiario_id,
        tipo="GES",
        regla_version_id=regla.regla_id,
        ventana_inicio=inicio,
        ventana_fin=fin,
        acumulado=filas[-1].acumulado,
        deducible_aplicado=deducible,
        fecha_cruce=fecha_cruce(filas, deducible),
        filas=filas,
    )

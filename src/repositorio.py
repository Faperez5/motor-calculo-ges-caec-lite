"""Read side of the engine: SQLite rows in, calculo dataclasses out.

Impure by design (it does I/O), but it only reads. The one thing it knows
about the rest of the engine is the dataclasses in calculo.
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

from src.calculo import Beneficiario, Prestacion, ReglaGES

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
SCHEMA = SQL_DIR / "schema.sql"
SEED = SQL_DIR / "seed.sql"


def conectar(path) -> sqlite3.Connection:
    """Open a database (":memory:" works) with foreign keys on.

    foreign_keys is per connection in SQLite, so it has to be set here,
    not only in schema.sql.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def inicializar(conn, schema=SCHEMA, seed=SEED) -> None:
    """Create the tables, then load the seed unless `seed` is None."""
    conn.executescript(Path(schema).read_text(encoding="utf-8"))
    if seed is not None:
        conn.executescript(Path(seed).read_text(encoding="utf-8"))


# ───────────────────────────────────────────────────────────
# Facts
# ───────────────────────────────────────────────────────────

def leer_beneficiario(conn, beneficiario_id: int) -> Beneficiario:
    fila = conn.execute(
        "SELECT * FROM beneficiarios WHERE beneficiario_id = ?", (beneficiario_id,)
    ).fetchone()
    if fila is None:
        raise LookupError(f"Beneficiary {beneficiario_id} does not exist")
    return Beneficiario(
        beneficiario_id=fila["beneficiario_id"],
        cotizacion_pactada_uf=fila["cotizacion_pactada_uf"],
        fecha_derivacion_caec=_fecha(fila["fecha_derivacion_caec"]),
    )


def leer_prestaciones(conn, beneficiario_id: int, conocido_hasta: datetime | None = None) -> list[Prestacion]:
    """All prestaciones of a beneficiary, as known at `conocido_hasta` (plan D5).

    None means "everything known now". The filter is on recibido_en
    (transaction time), never on fecha_prestacion.
    """
    sql = "SELECT * FROM prestaciones WHERE beneficiario_id = ?"
    params = [beneficiario_id]
    if conocido_hasta is not None:
        if not isinstance(conocido_hasta, datetime):
            raise TypeError("conocido_hasta must be a datetime, not a date: say which moment of the day")
        sql += " AND recibido_en <= ?"
        params.append(conocido_hasta.isoformat(timespec="seconds"))
    sql += " ORDER BY fecha_prestacion, prestacion_id"
    return [
        Prestacion(
            prestacion_id=f["prestacion_id"],
            beneficiario_id=f["beneficiario_id"],
            fecha_prestacion=date.fromisoformat(f["fecha_prestacion"]),
            recibido_en=datetime.fromisoformat(f["recibido_en"]),
            codigo_ges=f["codigo_ges"],
            en_red_ges=bool(f["en_red_ges"]),
            en_red_caec=bool(f["en_red_caec"]),
            monto_arancel=f["monto_arancel"],
            monto_copago=f["monto_copago"],
        )
        for f in conn.execute(sql, params)
    ]


# ───────────────────────────────────────────────────────────
# Rules and UF
# ───────────────────────────────────────────────────────────

def leer_reglas_ges(conn, codigo_ges: str) -> list[ReglaGES]:
    """Every version of a GES rule. The effective-dated pick happens in calculo."""
    return [
        ReglaGES(
            regla_id=f["regla_id"],
            codigo_ges=f["codigo_ges"],
            valid_from=date.fromisoformat(f["valid_from"]),
            valid_to=_fecha(f["valid_to"]),
            tope_cotizaciones=f["tope_cotizaciones"],
            tope_uf=f["tope_uf"],
        )
        for f in conn.execute(
            "SELECT * FROM parametros_ges WHERE codigo_ges = ? ORDER BY valid_from", (codigo_ges,)
        )
    ]


def leer_uf(conn, fecha: date) -> float:
    """UF value on exactly `fecha`. Missing → error; never guesses a nearby date."""
    fila = conn.execute("SELECT valor_clp FROM valores_uf WHERE fecha = ?", (fecha.isoformat(),)).fetchone()
    if fila is None:
        raise LookupError(f"No UF value for {fecha}")
    return fila["valor_clp"]


def _fecha(texto: str | None) -> date | None:
    return date.fromisoformat(texto) if texto is not None else None

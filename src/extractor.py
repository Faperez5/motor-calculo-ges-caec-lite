"""EXTRACT: SQLite (and, from phase 5, Excel) rows in, calculo dataclasses out.

Impure by design (it does I/O), but read-only: every write goes through
loader. The one thing it knows about the rest of the engine is the
dataclasses in calculo.
"""

import re
import sqlite3
import unicodedata
import zipfile
from datetime import date, datetime
from pathlib import Path

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from src.calculo import Beneficiario, FilaDesglose, Prestacion, ReglaGES, Resultado

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


def leer_valores_uf(conn) -> dict[date, float]:
    """The whole UF table, for calculo to pick from (it can't query)."""
    return {date.fromisoformat(f["fecha"]): f["valor_clp"] for f in conn.execute("SELECT * FROM valores_uf")}


# ───────────────────────────────────────────────────────────
# Results
# ───────────────────────────────────────────────────────────

def leer_resultado(conn, calculo_id: int) -> dict:
    """A saved result as stored: header columns plus a `detalle` list. For reports."""
    cab = conn.execute("SELECT * FROM calculos_resultado WHERE id = ?", (calculo_id,)).fetchone()
    if cab is None:
        raise LookupError(f"Result {calculo_id} does not exist")
    resultado = dict(cab)
    resultado["detalle"] = [
        dict(f) for f in conn.execute(
            """
            SELECT d.*, p.fecha_prestacion
            FROM calculos_detalle d JOIN prestaciones p USING (prestacion_id)
            WHERE d.calculo_id = ?
            ORDER BY p.fecha_prestacion, d.prestacion_id
            """,
            (calculo_id,),
        )
    ]
    return resultado


def leer_resultado_vigente(conn, beneficiario_id: int, tipo: str) -> tuple[int, Resultado] | None:
    """The current result rebuilt as a Resultado, so calculo can compare it with a new one."""
    cab = conn.execute(
        "SELECT id FROM calculos_resultado WHERE beneficiario_id = ? AND tipo = ? AND vigente = 1",
        (beneficiario_id, tipo),
    ).fetchone()
    if cab is None:
        return None
    r = leer_resultado(conn, cab["id"])
    return r["id"], Resultado(
        beneficiario_id=r["beneficiario_id"],
        tipo=r["tipo"],
        regla_version_id=r["regla_version_id"],
        ventana_inicio=date.fromisoformat(r["ventana_inicio"]),
        ventana_fin=date.fromisoformat(r["ventana_fin"]),
        acumulado=r["acumulado"],
        deducible_aplicado=r["deducible_aplicado"],
        fecha_cruce=_fecha(r["fecha_cruce"]),
        filas=tuple(
            FilaDesglose(
                prestacion_id=d["prestacion_id"],
                fecha=date.fromisoformat(d["fecha_prestacion"]),
                copago=d["copago"],
                paga_beneficiario=d["paga_beneficiario"],
                cubre_seguro=d["cubre_seguro"],
                acumulado=d["acumulado_hasta_aqui"],
            )
            for d in r["detalle"]
        ),
    )


def leer_historial(conn, beneficiario_id: int, tipo: str) -> list[dict]:
    """Every saved version for a beneficiary and tipo, oldest first."""
    return [dict(f) for f in conn.execute(
        """
        SELECT id, version_calculo, vigente, motivo_recalculo, regla_version_id, ventana_inicio,
               ventana_fin, deducible_aplicado, acumulado, fecha_cruce, calculado_en, conocido_hasta
        FROM calculos_resultado WHERE beneficiario_id = ? AND tipo = ? ORDER BY version_calculo
        """,
        (beneficiario_id, tipo),
    )]


def leer_ejecuciones(conn, limite: int = 20) -> list[dict]:
    """The last `limite` logged commands, newest first."""
    return [dict(f) for f in conn.execute("SELECT * FROM ejecuciones ORDER BY id DESC LIMIT ?", (limite,))]


def leer_ids_beneficiarios(conn) -> list[int]:
    return [f["beneficiario_id"] for f in conn.execute("SELECT beneficiario_id FROM beneficiarios ORDER BY 1")]


# ───────────────────────────────────────────────────────────
# Excel ingestion (phase 5)
# ───────────────────────────────────────────────────────────

OBLIGATORIOS = ("beneficiario_id", "fecha_prestacion", "en_red_caec", "monto_arancel", "monto_copago")
OPCIONALES = {"codigo_ges": None, "en_red_ges": True}     # value used when the column is absent or empty


def leer_excel_prestaciones(path, mapeo: dict[str, str]) -> tuple[list[dict], list[tuple[int, str]]]:
    """Read prestaciones from the first sheet of an .xlsx file.

    `mapeo` maps engine field → column header in this file format. Headers
    match ignoring case, accents and extra spaces. Returns (rows, rejected):
    rows are dicts of typed values (plus `fila_excel`, the row number) ready
    for loader.insertar_prestaciones;
    rejected is [(excel row number, reason)]. No row is dropped silently.
    A missing required column refuses the whole file (wrong format).
    """
    try:
        libro = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError) as e:
        raise ValueError(f"{path} is not a readable .xlsx file ({e})") from None
    try:
        filas_excel = libro.worksheets[0].iter_rows(values_only=True)
        encabezado = next(filas_excel, None)
        if encabezado is None:
            raise ValueError(f"{path} is empty")
        columnas = {_normalizar(h): i for i, h in enumerate(encabezado) if h is not None}
        faltan = [mapeo[c] for c in OBLIGATORIOS if _normalizar(mapeo[c]) not in columnas]
        if faltan:
            raise ValueError(f"{path} is missing columns {faltan}: wrong --formato?")
        indice = {campo: columnas.get(_normalizar(col)) for campo, col in mapeo.items()}

        filas, rechazadas = [], []
        for n, valores in enumerate(filas_excel, start=2):
            if all(v is None or str(v).strip() == "" for v in valores):
                continue
            try:
                filas.append({**_leer_fila(valores, indice), "fila_excel": n})
            except ValueError as e:
                rechazadas.append((n, str(e)))
        return filas, rechazadas
    finally:
        libro.close()


def _leer_fila(valores, indice) -> dict:
    fila = {}
    for campo, parser in _PARSERS.items():
        i = indice.get(campo)
        crudo = valores[i] if i is not None and i < len(valores) else None
        if crudo is None or (isinstance(crudo, str) and crudo.strip() == ""):
            if campo in OPCIONALES:
                fila[campo] = OPCIONALES[campo]
                continue
            raise ValueError(f"{campo}: missing")
        try:
            fila[campo] = parser(crudo)
        except ValueError as e:
            raise ValueError(f"{campo}: {e}") from None
    return fila


def _normalizar(texto) -> str:
    """'  Fecha  Prestación ' → 'fecha prestacion'."""
    sin_tildes = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.lower().split())


def _entero(v) -> int:
    if isinstance(v, bool):
        raise ValueError(f"can't read {v!r} as a number")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    raise ValueError(f"can't read {v!r} as a whole number")


def _fecha_excel(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for formato in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(v).strip(), formato).date()
        except ValueError:
            pass
    raise ValueError(f"can't read {v!r} as a date (YYYY-MM-DD, DD-MM-YYYY or DD/MM/YYYY)")


def _codigo(v) -> str:
    return str(v).strip().upper()


_SI, _NO = {"s", "si", "y", "yes", "true", "1", "x"}, {"n", "no", "false", "0"}


def _booleano(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    texto = _normalizar(v)
    if texto in _SI:
        return True
    if texto in _NO:
        return False
    raise ValueError(f"can't read {v!r} as yes/no")


def _monto(v) -> int:
    """CLP amount: 1234567, 1234567.0, '1.234.567', '$ 1.234.567', '1234567,00'. Never negative."""
    if isinstance(v, bool):
        raise ValueError(f"can't read {v!r} as an amount")
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not v.is_integer():
            raise ValueError(f"{v!r} has decimals; CLP amounts are whole pesos")
        monto = int(v)
    else:
        texto = str(v).replace("$", "").replace(" ", "").strip()
        texto = re.sub(r",0+$", "", texto)                      # '1.234.567,00'
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", texto):        # Chilean thousands separator
            texto = texto.replace(".", "")
        if not re.fullmatch(r"-?\d+", texto):
            raise ValueError(f"can't read {v!r} as an amount in pesos")
        monto = int(texto)
    if monto < 0:
        raise ValueError(f"{v!r} is negative")
    return monto


_PARSERS = {
    "beneficiario_id": _entero,
    "fecha_prestacion": _fecha_excel,
    "codigo_ges": _codigo,
    "en_red_ges": _booleano,
    "en_red_caec": _booleano,
    "monto_arancel": _monto,
    "monto_copago": _monto,
}


def _fecha(texto: str | None) -> date | None:
    return date.fromisoformat(texto) if texto is not None else None

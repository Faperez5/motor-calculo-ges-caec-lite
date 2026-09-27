"""Runner and CLI: extract → transform → load. No business logic lives here.

    python -m src.motor init     --db data/motor.db
    python -m src.motor calcular --db data/motor.db --beneficiario 1 --tipo GES
                                 [--codigo GES01] [--ahora 2026-09-27T12:00]
                                 [--conocido-hasta 2026-03-04T23:59]

Everything that comes from the outside world is decided here and passed in:
the current time (D8), the as-of cut (D5), the code version.
"""

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from src import calculo, extractor, loader

RAIZ = Path(__file__).resolve().parent.parent


def version_codigo() -> str:
    """Git commit of the running code, '-dirty' if tracked files have uncommitted changes."""
    try:
        git = lambda *a: subprocess.run(
            ["git", *a], cwd=RAIZ, capture_output=True, text=True, check=True
        ).stdout.strip()
        sha = git("rev-parse", "HEAD")
        sucio = git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return sha + ("-dirty" if sucio else "")


def calcular(conn, beneficiario_id: int, tipo: str, ahora: datetime,
             conocido_hasta: datetime | None = None, codigo_ges: str = "GES01",
             version: str = "unknown") -> int | None:
    """Run one calculation and save it. Returns the result id, or None if there was nothing to calculate.

    `conocido_hasta` defaults to `ahora`: a run dated in the past only sees
    the data that had arrived by then.
    """
    if tipo != "GES":
        raise NotImplementedError(f"tipo {tipo} arrives in S07")
    conocido_hasta = conocido_hasta or ahora
    if conocido_hasta > ahora:
        raise ValueError("conocido_hasta can't be later than ahora")

    # Extract
    beneficiario = extractor.leer_beneficiario(conn, beneficiario_id)
    prestaciones = extractor.leer_prestaciones(conn, beneficiario_id, conocido_hasta)
    reglas = extractor.leer_reglas_ges(conn, codigo_ges)
    valores_uf = extractor.leer_valores_uf(conn)

    # Transform
    resultado = calculo.calcular_ges(beneficiario, prestaciones, reglas, codigo_ges, valores_uf)
    if resultado is None:
        return None

    # Load
    return loader.guardar_resultado(conn, resultado, "inicial", ahora, conocido_hasta, version)


# ───────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────

def _args(argv):
    ap = argparse.ArgumentParser(prog="python -m src.motor", description="Motor GES/CAEC lite")
    sub = ap.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("init", help="create a new database with schema + seed")
    p.add_argument("--db", required=True)
    p.add_argument("--sin-seed", action="store_true", help="tables only, no synthetic data")

    p = sub.add_parser("calcular", help="calculate and save one result")
    p.add_argument("--db", required=True)
    p.add_argument("--beneficiario", type=int, required=True)
    p.add_argument("--tipo", choices=["GES"], required=True)
    p.add_argument("--codigo", default="GES01", help="GES problem code (default: GES01)")
    p.add_argument("--ahora", type=datetime.fromisoformat, help="calculation time (default: now)")
    p.add_argument("--conocido-hasta", type=datetime.fromisoformat,
                   help="only use data received up to this moment (default: --ahora)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _args(argv)
    db = Path(args.db)

    if args.comando == "init":
        if db.exists():
            print(f"error: {db} already exists. Delete it to start over.", file=sys.stderr)
            return 1
        conn = extractor.conectar(db)
        try:
            extractor.inicializar(conn, seed=None if args.sin_seed else extractor.SEED)
        finally:
            conn.close()
        print(f"created {db}" + ("" if args.sin_seed else " with seed data"))
        return 0

    if not db.exists():
        print(f"error: {db} doesn't exist. Run `init` first.", file=sys.stderr)
        return 1
    conn = extractor.conectar(db)
    try:
        ahora = args.ahora or datetime.now().replace(microsecond=0)
        cid = calcular(conn, args.beneficiario, args.tipo, ahora, args.conocido_hasta,
                       args.codigo, version_codigo())
        if cid is None:
            print(f"beneficiary {args.beneficiario} has no qualifying {args.codigo} copagos: nothing to calculate")
            return 0
        print(loader.reporte_texto(extractor.leer_resultado(conn, cid)))
        return 0
    except (LookupError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except sqlite3.IntegrityError:
        print("error: a current result already exists for this beneficiary and tipo. "
              "Recalculation arrives in S11.", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

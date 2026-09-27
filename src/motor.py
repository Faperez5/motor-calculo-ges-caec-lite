"""Runner and CLI: extract → transform → load. No business logic lives here.

    python -m src.motor init     --db data/motor.db
    python -m src.motor calcular --db data/motor.db --beneficiario 1 --tipo GES
                                 [--codigo GES01] [--ahora 2026-09-27T12:00]
                                 [--conocido-hasta 2026-03-04T23:59]
    python -m src.motor recalcular-todos --db data/motor.db --tipo GES
    python -m src.motor cargar-regla-ges --db data/motor.db --codigo GES01 --desde 2025-03-01
                                 --tope-uf 125 --fuente "Decreto X"
    python -m src.motor historial --db data/motor.db --beneficiario 1 --tipo GES
    python -m src.motor ejecuciones --db data/motor.db

Everything that comes from the outside world is decided here and passed in:
the current time (D8), the as-of cut (D5), the code version.

Every command that can write is logged in `ejecuciones` (who, when, exact
command line, code version, outcome), and what it writes points back to it.
"""

import argparse
import getpass
import shlex
import sqlite3
import subprocess
import sys
from datetime import date, datetime
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
             version: str = "unknown", ejecucion_id: int | None = None) -> tuple[int, str] | None:
    """Run one calculation and save it if it changed. Returns (result id, motivo).

    With motivo 'sin_cambios' nothing is written and the id is the current
    result's. None means there was nothing to calculate.

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

    vigente = extractor.leer_resultado_vigente(conn, beneficiario_id, tipo)
    anterior_id, anterior = vigente or (None, None)

    # Transform
    resultado = calculo.calcular_ges(beneficiario, prestaciones, reglas, codigo_ges, valores_uf)
    if resultado is None:
        return None
    motivo = calculo.motivo_recalculo(anterior, resultado)
    if motivo == "sin_cambios":
        return anterior_id, motivo
    avisos = calculo.chequear_datos(prestaciones, reglas)

    # Load
    cid = loader.guardar_resultado(conn, resultado, motivo, ahora, conocido_hasta, version, avisos, ejecucion_id)
    return cid, motivo


# ───────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────

def _args(argv):
    ap = argparse.ArgumentParser(prog="python -m src.motor", description="Motor GES/CAEC lite")
    sub = ap.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("init", help="create a new database with schema + seed")
    p.add_argument("--db", required=True)
    p.add_argument("--sin-seed", action="store_true", help="tables only, no synthetic data")

    def calculo_args(p):
        p.add_argument("--db", required=True)
        p.add_argument("--tipo", choices=["GES"], required=True)
        p.add_argument("--codigo", default="GES01", help="GES problem code (default: GES01)")
        p.add_argument("--ahora", type=datetime.fromisoformat, help="calculation time (default: now)")
        p.add_argument("--conocido-hasta", type=datetime.fromisoformat,
                       help="only use data received up to this moment (default: --ahora)")

    p = sub.add_parser("calcular", help="calculate one beneficiary; saves a new version only if it changed")
    p.add_argument("--beneficiario", type=int, required=True)
    calculo_args(p)

    p = sub.add_parser("recalcular-todos", help="calculate every beneficiary")
    calculo_args(p)

    p = sub.add_parser("cargar-regla-ges", help="load a new GES rule version (future or retroactive)")
    p.add_argument("--db", required=True)
    p.add_argument("--codigo", required=True)
    p.add_argument("--desde", type=date.fromisoformat, required=True, help="valid_from, YYYY-MM-DD")
    p.add_argument("--tope-uf", type=float, required=True)
    p.add_argument("--tope-cotizaciones", type=int, default=29)
    p.add_argument("--fuente", required=True, help="where the rule comes from, e.g. the decree")
    p.add_argument("--cargado-por", default=getpass.getuser())
    p.add_argument("--ahora", type=datetime.fromisoformat, help="load time (default: now)")

    p = sub.add_parser("historial", help="every saved version for a beneficiary")
    p.add_argument("--db", required=True)
    p.add_argument("--beneficiario", type=int, required=True)
    p.add_argument("--tipo", choices=["GES"], required=True)

    p = sub.add_parser("ejecuciones", help="the log of commands run against this database")
    p.add_argument("--db", required=True)
    p.add_argument("--ultimas", type=int, default=20, help="how many to show (default: 20)")
    return ap.parse_args(argv)


ESCRIBEN = {"init", "calcular", "recalcular-todos", "cargar-regla-ges", "ingerir"}  # logged in ejecuciones


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    args = _args(argv)
    db = Path(args.db)
    if args.comando == "init" and db.exists():
        print(f"error: {db} already exists. Delete it to start over.", file=sys.stderr)
        return 1
    if args.comando != "init" and not db.exists():
        print(f"error: {db} doesn't exist. Run `init` first.", file=sys.stderr)
        return 1

    conn = extractor.conectar(db)
    try:
        if args.comando == "init":
            extractor.inicializar(conn, seed=None if args.sin_seed else extractor.SEED)
        version = version_codigo()
        eid = None
        if args.comando in ESCRIBEN:
            eid = loader.iniciar_ejecucion(conn, "python -m src.motor " + shlex.join(argv),
                                           getpass.getuser(), version, _reloj())
        try:
            codigo, mensaje = _ejecutar(conn, args, version, eid)
        except (LookupError, ValueError, sqlite3.IntegrityError) as e:
            print(f"error: {e}", file=sys.stderr)
            codigo, mensaje = 1, str(e)
        except Exception as e:  # a bug: log it, then let it crash loudly
            if eid:
                loader.terminar_ejecucion(conn, eid, "error", f"{type(e).__name__}: {e}", _reloj())
            raise
        if eid:
            loader.terminar_ejecucion(conn, eid, "ok" if codigo == 0 else "error", mensaje, _reloj())
        return codigo
    finally:
        conn.close()


def _reloj() -> datetime:
    """Wall-clock time for the run log. Unlike --ahora, never overridden."""
    return datetime.now().replace(microsecond=0)


def _ejecutar(conn, args, version: str, eid: int | None) -> tuple[int, str | None]:
    """Run one command. Prints for the user; returns (exit code, one-line outcome for the log)."""
    ahora = getattr(args, "ahora", None) or _reloj()

    if args.comando == "init":
        semilla = "" if args.sin_seed else " with seed data"
        print(f"created {args.db}{semilla}")
        return 0, f"created{semilla}"

    if args.comando == "calcular":
        salida = calcular(conn, args.beneficiario, args.tipo, ahora, args.conocido_hasta,
                          args.codigo, version, eid)
        if salida is None:
            print(f"beneficiary {args.beneficiario} has no qualifying {args.codigo} copagos: nothing to calculate")
            return 0, "nothing to calculate"
        cid, motivo = salida
        if motivo == "sin_cambios":
            print(f"no changes since result #{cid}: nothing saved\n")
        print(loader.reporte_texto(extractor.leer_resultado(conn, cid)))
        return 0, f"{motivo} → result #{cid}"

    if args.comando == "recalcular-todos":
        return _recalcular_todos(conn, args, ahora, version, eid)

    if args.comando == "cargar-regla-ges":
        rid = loader.cargar_regla_ges(conn, args.codigo, args.desde, args.tope_cotizaciones, args.tope_uf,
                                      args.fuente, args.cargado_por, ahora, eid)
        print(f"loaded rule {rid}. {args.codigo} versions now:")
        for r in extractor.leer_reglas_ges(conn, args.codigo):
            print(f"  {r.regla_id:>3}  {r.valid_from} → {str(r.valid_to or 'open'):<10}  tope {r.tope_uf} UF")
        print("run `recalcular-todos` to apply it to saved results")
        return 0, f"loaded rule {rid}"

    if args.comando == "historial":
        print(loader.reporte_historial(extractor.leer_historial(conn, args.beneficiario, args.tipo)))
        return 0, None

    if args.comando == "ejecuciones":
        print(loader.reporte_ejecuciones(extractor.leer_ejecuciones(conn, args.ultimas)))
        return 0, None

    raise ValueError(f"unknown command {args.comando}")


def _recalcular_todos(conn, args, ahora, version, eid) -> tuple[int, str]:
    """Calculate every beneficiary; one line each. Keeps going past errors, exit 1 if any."""
    cuenta: dict[str, int] = {}
    for bid in extractor.leer_ids_beneficiarios(conn):
        try:
            salida = calcular(conn, bid, args.tipo, ahora, args.conocido_hasta, args.codigo, version, eid)
        except (LookupError, ValueError) as e:
            cuenta["error"] = cuenta.get("error", 0) + 1
            print(f"  beneficiary {bid}: ERROR {e}")
            continue
        if salida is None:
            clave = "nothing to calculate"
            print(f"  beneficiary {bid}: nothing to calculate")
        else:
            cid, clave = salida
            print(f"  beneficiary {bid}: {clave:<18} result #{cid}")
        cuenta[clave] = cuenta.get(clave, 0) + 1
    resumen = ", ".join(f"{n} {k}" for k, n in sorted(cuenta.items()))
    return (1 if "error" in cuenta else 0), resumen


if __name__ == "__main__":
    sys.exit(main())

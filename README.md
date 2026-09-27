# Motor de cálculo GES/CAEC — lite

Practice project: an engine for GES and CAEC deductibles with time-versioned rules
and auditable, versioned results. Synthetic data, simplified rules; not a production
or regulatory reference.

- Spec: [`motor_calculo_ges_caec_spec.md`](motor_calculo_ges_caec_spec.md)
- Build plan: [`deployment_plan.md`](deployment_plan.md)

## Setup

Requires Python 3.12.

```bash
python3.12 -m venv .venv        # or: uv venv --python 3.12 --seed .venv
. .venv/bin/activate
pip install -r requirements.txt       # engine + tests
pip install -r requirements-dev.txt   # optional: adds ipykernel for runner.ipynb
python -m pytest -q
```

## Run

```bash
python -m src.motor init     --db data/motor.db
python -m src.motor calcular --db data/motor.db --beneficiario 1 --tipo GES
python -m src.motor calcular --help        # --codigo, --ahora, --conocido-hasta

# load a rule change (future or retroactive), then apply it everywhere
python -m src.motor cargar-regla-ges --db data/motor.db --codigo GES01 --desde 2025-03-01 \
                                     --tope-uf 125 --fuente "Decreto X"
python -m src.motor recalcular-todos --db data/motor.db --tipo GES

# every saved version of a result, and why it changed
python -m src.motor historial --db data/motor.db --beneficiario 1 --tipo GES

# load prestaciones from an Excel export (format A: clinic, B: insurer), then recalculate
python -m src.motor ingerir --db data/motor.db --archivo data/export.xlsx --formato A
python -m src.motor recalcular-todos --db data/motor.db --tipo GES

# every command that changed the database: who, when, exact command line, code version, outcome
python -m src.motor ejecuciones --db data/motor.db
```

`ingerir` never drops a row silently: rows it can't read (bad date, missing amount,
unknown beneficiary, duplicate of a stored prestación) are listed with their Excel row
number and reason, and the rest are loaded. The column layouts live in `FORMATOS` in
`src/motor.py`.

`calcular` prints the audit trace of the saved result: rule version, derived window,
deductible, crossing date, what data the run knew, the git commit that ran, data
warnings, and the per-prestación split between beneficiary and insurer.

Results are never overwritten. A recalculation saves a new version with its reason
(`datos_tardios`, `ventana_desplazada`, `cambio_normativo`, `correccion`) and marks the
old one as superseded; if nothing changed, nothing is saved.

## Runner notebook

[`runner.ipynb`](runner.ipynb) runs the test suite and calls the engine's functions
interactively. In VS Code, open it and pick the `.venv` interpreter as the kernel.

## Status

Phases 1, 3, 4 and 5 done (`v0.5-ingesta`): GES end to end — extract (`src/extractor.py`) → calculate (`src/calculo.py`) → load (`src/loader.py`), run by `src/motor.py` — with a derived window, versioned results, safe rule loading, Excel ingestion and a run log. CAEC (phase 2) is deferred. See the build plan for what comes next.

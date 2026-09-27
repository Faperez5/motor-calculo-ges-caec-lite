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
```

The output is the audit trace of the saved result: rule version, derived window,
deductible, crossing date, what data the run knew, the git commit that ran, and the
per-prestación split between beneficiary and insurer.

## Runner notebook

[`runner.ipynb`](runner.ipynb) runs the test suite and calls the engine's functions
interactively. In VS Code, open it and pick the `.venv` interpreter as the kernel.

## Status

Phase 1 done (`v0.1-slice`): GES end to end — extract (`src/extractor.py`) → calculate (`src/calculo.py`) → load (`src/loader.py`), run by `src/motor.py`. See the build plan for what comes next.

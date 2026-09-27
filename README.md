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

## Runner notebook

[`runner.ipynb`](runner.ipynb) runs the test suite and calls the engine's functions
interactively. In VS Code, open it and pick the `.venv` interpreter as the kernel.

## Status

Phase 1 in progress: pure core (`src/calculo.py`) and database layer (`src/repositorio.py`, `src/persistencia.py`) done. See the build plan for what comes next.

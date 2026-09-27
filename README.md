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
pip install -r requirements.txt
python -m pytest -q
```

## Status

Phase 0 (bootstrap). See the build plan for what comes next.

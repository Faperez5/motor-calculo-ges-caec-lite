# Deployment plan — Motor GES/CAEC (lite)

> **Source spec:** `motor_calculo_ges_caec_spec.md` (sections cited as §n).
> **Scope:** lite version. Plain Python + SQLite, no containers.
> **Target repo:** `github.com/Faperez5/motor-calculo-ges-caec-lite` (new, separate from the full `motor-calculo-ges-caec`).

---

## 0. Ground rules

| Rule | What it means in practice |
|---|---|
| **Small commits** | Each step is one commit: the code change plus its tests. Commit message: `S<nn>: <what>`. |
| **Few files** | 14 files total (§1). Helpers go in an existing module. |
| **Notebook is a front end** | `runner.ipynb` only calls code in `src/`; it holds no logic of its own. Clear its outputs before committing. Each step that adds something runnable adds a cell. |
| **Pure core** | `calculo.py` has no `sqlite3`, no `open()`, no `datetime.now()`, and doesn't import `extractor` or `loader`. A test enforces this. |
| **Suite stays green** | `python -m pytest -q` before every push. |

`python -m pytest` (not bare `pytest`) puts the repo root on `sys.path`, so `from src import calculo` works without a `conftest.py` or `pytest.ini`.

### Why no Docker

This is a local batch CLI with two dependencies and a SQLite file. Reproducibility (§3.3, §9) comes from:

- **Pinned versions** in `requirements.txt`, generated with `pip freeze` so transitive deps are pinned too.
- **One Python version** (3.12), stated in the README and used by CI.
- **Rebuildable database**: `data/motor.db` is never committed; it's rebuilt from `schema.sql` + `seed.sql`.
- **Git tags** at the end of each phase, so any past result can be re-run from its tag.

A container adds a file, a build step, and a tool to install, and it doesn't buy anything those four don't already cover at this scale.

**Keeping Docker easy to add later.** Docker only wraps the app, so adding it later means adding a `Dockerfile` (§9) and a `.dockerignore`, with no Python changes, as long as: every path comes in as a CLI flag (`--db`, `--archivo`), nothing is hardcoded to this machine, every dependency is pinned in `requirements.txt`, and no config lives outside the repo. Add it if the project moves to Postgres, gains collaborators, or has to run on a machine you don't control.

---

## 1. Target layout

```
motor-ges-caec-lite/
├── README.md
├── requirements.txt            # pinned (pip freeze): what the engine needs
├── requirements-dev.txt        # + ipykernel, to run the notebook
├── runner.ipynb                # interactive runner: tests + calls into src/
├── .gitignore
├── .github/workflows/ci.yml    # phase 6
├── sql/
│   ├── schema.sql              # DDL (§4)
│   └── seed.sql                # synthetic rules, UF, beneficiaries, prestaciones
├── src/
│   ├── __init__.py             # empty
│   ├── extractor.py            # EXTRACT   — reads SQLite / Excel (read-only)
│   ├── calculo.py              # TRANSFORM — pure: rules, deductibles, window, split, validation
│   ├── loader.py               # LOAD      — every DB write: versioned results, rules, ingested rows; later Excel/Word reports
│   └── motor.py                # CLI — runs extractor → calculo → loader
└── tests/
    ├── test_calculo.py         # pure functions, no DB
    └── test_motor.py           # end to end on in-memory SQLite
```

Compared with the spec (§3.2): `reglas.py` and `validacion.py` fold into `calculo.py` (they're pure functions over in-memory lists), the three test files become two, and `data/sintetico/` generators become one fixed `seed.sql`.

It's an ETL pipeline: **extract** (`extractor`), **transform** (`calculo`), **load** (`loader`), run by `motor`.

```
motor.py ──► extractor.py ──► sqlite3 / openpyxl
   ├──────► calculo.py          (stdlib only)
   └──────► loader.py ──► sqlite3 (later: Excel / Word reports)
```

`calculo` imports none of the others. `extractor` never writes and never imports `loader`. `loader` may read through `extractor` (e.g. to validate the rule set after loading a rule).

---

## 2. Decisions the spec leaves open

Defaults for lite. List them in the README.

| # | Question | Decision |
|---|---|---|
| D1 | Rule for a window that crosses a rule change | The rule in force on `ventana_inicio` applies to the whole window (one `regla_version_id` per result). |
| D2 | Window starts at the chronologically first copago, or the first one known? (§5.3) | Chronological. The "as known at" view comes from `conocido_hasta` (D5). |
| D3 | UF value for converting a deductible to CLP | UF on the last day of the month before `ventana_inicio`, for both GES and CAEC. One conversion per window. |
| D4 | Money type | CLP as `INTEGER`, UF as `REAL`. Round once, when converting UF → CLP, in `calculo.py`. |
| D5 | Transaction-time queries | `leer_prestaciones(beneficiario_id, conocido_hasta)` filters on `recibido_en <= conocido_hasta`. |
| D6 | GES network flag | Add `en_red_ges BOOLEAN NOT NULL DEFAULT 1` to `prestaciones`. |
| D7 | GES multi-event (43 cotizaciones / UF 181) | Out of scope. |
| D8 | Calculation date | Passed in via `--ahora` (default: now). Never read inside `calculo.py`. |
| D9 | Period close (§6) | Not implemented. Every recalculation creates a new version. |
| D10 | How rules get into the database | Only through `loader.cargar_regla` (S11), which closes the previous version, fills `cargado_por`/`cargado_en`, runs `validar_reglas`, and rolls back on failure. No manual `INSERT`/`UPDATE` on rule tables (§3.3), and no Excel "master" that gets edited in place. Excel is fine as an **input format** (one file or row per change) fed through the same loader (S14). |
| D11 | Window ending when it starts on Feb 29 | The window ends the day before the anniversary; a Feb 29 start's anniversary is Mar 1, so 2024-02-29 → 2025-02-28 (the spec formula in §7.5 would give 2025-02-27). |

---

## 3. Steps

### Phase 0 — Bootstrap

| Step | Content | Done when |
|---|---|---|
| **S00** | `git init`, `gh repo create Faperez5/motor-calculo-ges-caec-lite --private --source . --push`. Add `.gitignore` (`__pycache__/`, `.venv/`, `.pytest_cache/`, `*.db`, `data/*.xlsx`), `requirements.txt` (`pytest`, then `pip freeze`; `python-dateutil` was dropped in S08), a README stub, and the spec + this plan. | Remote exists; fresh venv installs cleanly. |

### Phase 1 — GES vertical slice

| Step | Content | Done when |
|---|---|---|
| **S01** | `sql/schema.sql`: every table from §4 plus D4, D6, `CHECK (monto_copago >= 0)`, `CHECK (valid_to IS NULL OR valid_from < valid_to)`, `PRAGMA foreign_keys = ON`. | `sqlite3 :memory: < sql/schema.sql` runs. |
| **S02** | `sql/seed.sql`: `GES01` in two versions (2025: UF 122, closed 2025-12-31; 2026: UF 130, open), one CAEC rule, month-end UF 2025-01 → 2026-12, 2 beneficiaries, ~10 prestaciones including one late arrival. | Loads on top of the schema. |
| **S03** | `src/calculo.py` + `tests/test_calculo.py` + `runner.ipynb` + `requirements-dev.txt`: dataclasses `ReglaGES`, `ReglaCAEC`, `Prestacion`; `regla_vigente`, `deducible_ges_uf`, `uf_a_clp`, `desglose` (§7.4). Tests: rule boundary (§8.2), gap returns `None`, split invariants, idempotence, and the **`ast` guard** on `calculo.py` imports. | Green. |
| **S04** | `src/extractor.py` + `src/loader.py`: `conectar`, `inicializar`, `leer_beneficiario`, `leer_prestaciones` (D5), `leer_reglas_ges`, `leer_uf(fecha)` (raises if missing; GES needs it in S05); `guardar_resultado` writes header + detail in one transaction (`version_calculo = 1` for now). `calculo.py` gains the data it needs: `Beneficiario`, `Resultado`, `fecha_cruce`. DB tests in `test_motor.py` on in-memory SQLite. | Green. |
| **S05** | `src/motor.py` + tests: CLI `init --db` and `calcular --db --beneficiario --tipo GES [--codigo] [--ahora] [--conocido-hasta]`. `motor` only wires E → T → L; the GES pipeline is one pure call, `calculo.calcular_ges` (filter → derived window → rule on `ventana_inicio` → deductible → split), with `derivar_ventana`, `en_ventana`, `fecha_uf` (D3). `calculos_resultado` gains `conocido_hasta` (defaults to `--ahora`) and `version_codigo` (git commit, `-dirty` if uncommitted). The trace is printed by `loader.reporte_texto` from the **saved** result. End-to-end tests on in-memory SQLite + CLI tests. | Green. **Tag `v0.1-slice`.** |

### Phase 2 — CAEC *(deferred)*

Skipped for now; phases 3–4 don't depend on it. Pick it up by doing S06–S07 as written, then add `--tipo CAEC` to `recalcular-todos`.


| Step | Content | Done when |
|---|---|---|
| **S06** | `calculo.py`: `deducible_caec_uf` with `max(piso, min(bruto, techo))` (§7.3), `filtrar_caec` (in network, on/after referral), `calcular_caec` (reuses `derivar_ventana` and `fecha_uf`). Tests: 3.5 → 105, 1.0 → 60, 10.0 → 126, multi-event → 181, filter cases. | Green. |
| **S07** | `extractor.py`: `leer_reglas_caec`. `motor.py`: `--tipo CAEC`. End-to-end test for beneficiary 2. | Green. **Tag `v0.2-caec`.** |

### Phase 3 — Derived window (§5.3)

| Step | Content | Done when |
|---|---|---|
| **S08** | `calculo.py`: window-shift tests on top of the `derivar_ventana` built in S05: an earlier late arrival moves the window back, drops a prestación off the end, and moves the crossing date. Feb 29 rule (D11), which also drops `python-dateutil` from the engine's requirements. | Green. **Tag `v0.3-ventana`.** |
| ~~S09~~ | *Dropped.* It merged `calcular_ges` and `calcular_caec`; with CAEC deferred there's nothing to merge. Revisit with phase 2. | — |

### Phase 4 — Recalculation and versioning (§6)

| Step | Content | Done when |
|---|---|---|
| **S10** | `calculo.py`: `motivo_recalculo(anterior, nuevo)`, `validar_reglas` (§8.1), `chequear_datos` (§8.3). One test per reason and per validation failure. | Green. |
| **S11** | `loader.py`: versioned `guardar_resultado` (mark previous `vigente = 0`, insert `prev + 1`; write nothing if unchanged) and `cargar_regla` (close open version, insert, validate, roll back on failure). `extractor.py`: `leer_resultado_vigente`. | — |
| **S12** | `motor.py`: `cargar-regla-ges`, `recalcular-todos`, `calcular` stores `motivo`. `test_motor.py`: the four scenarios — (1) no change → no new version; (2) late prestación inside window → `datos_tardios` with the **historical** rule; (3) late prestación before window → `ventana_desplazada`; (4) retroactive rule → `cambio_normativo`, old row kept with `vigente = 0`. | Green. **Tag `v0.4-recalculo`.** |

### Phase 5 — Excel ingestion

| Step | Content | Done when |
|---|---|---|
| **S13** | Add `openpyxl` to `requirements.txt` (re-freeze). `extractor.py`: `leer_excel_prestaciones(path, mapeo)` returning rows + `rechazadas`. `loader.py`: append-only `insertar_prestaciones`. `motor.py`: `ingerir --archivo --formato A\|B`. Test builds a tiny `.xlsx` in `tmp_path`, ingests, checks rejects, recalculates → `datos_tardios`. | Green. |
| **S14** | *(optional, when someone non-technical maintains rules)* `extractor.py`: `leer_excel_reglas(path)`. `motor.py`: `cargar-reglas --archivo x.xlsx --cargado-por <nombre>`, which sends each row through `cargar_regla` (D10). The whole file loads in one transaction or not at all. Test: a valid file loads and closes the previous version; a file that creates an overlap loads nothing. | Green. **Tag `v0.5-ingesta`.** |

### Phase 6 — CI and wrap-up

| Step | Content | Done when |
|---|---|---|
| **S15** | `.github/workflows/ci.yml`: on push/PR, checkout → setup-python 3.12 → `pip install -r requirements.txt` → `python -m pytest -q`. | Green badge. |
| **S16** | README final pass: badge, quick start, the four recalculation scenarios as a demo, D1–D10, what's not done. | **Tag `v1.0-lite`.** |

---

## 4. How it runs

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt       # or requirements-dev.txt, to use runner.ipynb
python -m src.motor init --db data/motor.db
python -m src.motor calcular --db data/motor.db --beneficiario 1 --tipo GES
python -m pytest -q
```

Each saved result records `version_codigo` (the git commit) and `conocido_hasta`, so it says which code and which data produced it.

To reproduce an old result: `git checkout v0.x-…`, recreate the venv, re-run.

---

## 5. Summary

| Phase | Steps | Tag |
|---|---|---|
| 0 Bootstrap | S00 | — |
| 1 GES slice | S01–S05 | `v0.1-slice` |
| 2 CAEC *(deferred)* | S06–S07 | `v0.2-caec` |
| 3 Window | S08 | `v0.3-ventana` |
| 4 Recalculation | S10–S12 | `v0.4-recalculo` |
| 5 Excel | S13–S14 | `v0.5-ingesta` |
| 6 CI | S15–S16 | `v1.0-lite` |

17 commits, 14 files, 3 layers, 1 SQLite database, no containers.

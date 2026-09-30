# User guide — Motor GES/CAEC (lite)

> How to use the engine and the rules it applies.
> Design and rationale: `motor_calculo_ges_caec_spec.md`. Build steps and decisions D1–D12: `deployment_plan.md`.

**Scope today:** GES only (`--tipo GES`). CAEC is designed but not built (plan phase 2).

---

## 1. Setup

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # add requirements-dev.txt for runner.ipynb
python -m src.motor init --db data/motor.db
```

Run every command from the project root. Every subcommand has `--help`.

---

## 2. Commands

| Command | Writes? | What it does |
|---|---|---|
| `init` | yes | Creates a new database: schema + synthetic seed data |
| `calcular` | yes, if changed | Calculates one beneficiary and saves a new version only if the result changed |
| `recalcular-todos` | yes, if changed | Runs `calcular` for every beneficiary |
| `cargar-regla-ges` | yes | Loads a new version of a GES rule (future or retroactive) |
| `ingerir` | yes | Loads prestaciones from an Excel export |
| `ver` | no | One beneficiary's data: details, prestaciones (and when each arrived), current results |
| `historial` | no | Every saved version of one beneficiary's result, and why each changed |
| `ejecuciones` | no | The run log: every command that wrote to the database |

### `init`

```bash
python -m src.motor init --db data/motor.db [--sin-seed]
```

- Refuses to run if the file already exists. To start over, delete the file.
- `--sin-seed` creates the tables only, with no data.

### `calcular`

```bash
python -m src.motor calcular --db data/motor.db --beneficiario 1 --tipo GES \
    [--codigo GES01] [--ahora 2026-09-27T12:00] [--conocido-hasta 2026-03-04T23:59] [--simular]
```

| Option | Default | Meaning |
|---|---|---|
| `--codigo` | `GES01` | GES problem code |
| `--ahora` | now | Calculation time stored on the result (D8) |
| `--conocido-hasta` | `--ahora` | Only uses prestaciones with `recibido_en <=` this moment (D5). Can't be later than `--ahora` |
| `--simular` | off | Calculates and prints, saves nothing, and isn't logged. The header reads `SIMULATION, NOT SAVED` and shows the motivo the result would have |

Prints the audit trace of the saved result: rule version, window, deductible, crossing date, what data the run saw, the git commit, any data warnings, and the split for each prestación.

If nothing changed, it prints `no changes since result #N: nothing saved` and shows the current result.

**Looking at the past.** To see what the engine would have said with only the data received by a given moment, use `--simular` with `--conocido-hasta`:

```bash
python -m src.motor calcular --db data/motor.db --beneficiario 1 --tipo GES --simular --conocido-hasta 2026-03-04T23:59
```

Without `--simular`, a `--conocido-hasta` earlier than the current result's is refused: saving it would replace a result with one that knew less.

### `recalcular-todos`

```bash
python -m src.motor recalcular-todos --db data/motor.db --tipo GES [--codigo] [--ahora] [--conocido-hasta]
```

Takes the same options as `calcular`. Prints one line per beneficiary: the reason and result id, `nothing to calculate`, or `ERROR`. It keeps going past errors and exits with code 1 if any beneficiary failed. Run it after `cargar-regla-ges` or `ingerir`.

### `cargar-regla-ges`

```bash
python -m src.motor cargar-regla-ges --db data/motor.db --codigo GES01 --desde 2025-03-01 \
    --tope-uf 125 --fuente "Decreto X" [--tope-cotizaciones 29] [--cargado-por <user>] [--ahora]
```

- `--desde` is the new version's `valid_from`. It can be in the future or in the past (retroactive).
- The version that contains `--desde` is closed the day before. The new version takes over until that version's old end date. If `--desde` falls before every existing version, the new one runs until the first of them.
- Refused if a version already starts on the same date. Correcting an existing version is out of scope.
- After loading, the whole rule set is validated (§5), and if anything is wrong the load is rolled back.
- Existing results aren't changed. Run `recalcular-todos` to apply the new rule.

### `ingerir`

```bash
python -m src.motor ingerir --db data/motor.db --archivo data/export.xlsx --formato A|B [--ahora]
```

Reads the first sheet of an `.xlsx` file and appends the rows as prestaciones, with `recibido_en = --ahora`. Rules are in §6. Existing prestaciones are never updated. Run `recalcular-todos` afterwards.

### `ver`

```bash
python -m src.motor ver --db data/motor.db --beneficiario 1 [--conocido-hasta 2026-03-04T23:59]
```

Shows what the database holds for one beneficiary. Read-only, so it isn't logged.
- The beneficiary: cotización pactada and CAEC referral date.
- Every prestación: service date, when it was received, GES code, networks, arancel, copago, and which run loaded it (`seed`, or `run #N` from `ingerir`). A received date long after the service date marks a late arrival.
- The current result per type. The full trace is in `calcular`, and every version is in `historial`.

`--conocido-hasta` lists only the prestaciones received by that moment. The results shown are always the current ones.

### `historial` and `ejecuciones`

```bash
python -m src.motor historial   --db data/motor.db --beneficiario 1 --tipo GES
python -m src.motor ejecuciones --db data/motor.db [--ultimas 20]
```

`historial` lists every version, with `*` marking the current one. `ejecuciones` shows, for each writing command: when it started, the user, `ok`/`error`/`en_curso`, the code version (`*` = uncommitted changes), the exact command line, and its outcome.

---

## 3. Typical workflows

```bash
# first calculation
python -m src.motor calcular --db data/motor.db --beneficiario 1 --tipo GES

# a decree changes the tope retroactively
python -m src.motor cargar-regla-ges --db data/motor.db --codigo GES01 --desde 2025-03-01 --tope-uf 125 --fuente "Decreto X"
python -m src.motor recalcular-todos --db data/motor.db --tipo GES      # → cambio_normativo

# a new export arrives from the clinic
python -m src.motor ingerir --db data/motor.db --archivo data/export.xlsx --formato A
python -m src.motor recalcular-todos --db data/motor.db --tipo GES      # → datos_tardios or ventana_desplazada

# why did the number change?
python -m src.motor historial --db data/motor.db --beneficiario 1 --tipo GES
```

---

## 4. How a GES result is calculated

`calculo.calcular_ges` runs these steps in order. All of them are pure functions in `src/calculo.py`.

| # | Step | Rule |
|---|---|---|
| 1 | **Filter** | Only prestaciones with the requested `codigo_ges` and `en_red_ges = 1` count (spec §5.1) |
| 2 | **Window** | Starts on the date of the chronologically first qualifying copago (D2). Ends the day before its anniversary. A start on Feb 29 ends on Feb 28 of the next year (D11). The window is derived, not stored as a parameter: an earlier late arrival moves it |
| 3 | **Rule** | The rule version in force on `ventana_inicio` applies to the whole window (D1). Validity dates are inclusive |
| 4 | **Deductible (UF)** | `min(tope_cotizaciones × cotización pactada, tope_uf)` |
| 5 | **Deductible (CLP)** | UF value on the last day of the month before `ventana_inicio` (D3). Rounded once, to whole pesos (D4) |
| 6 | **Split** | In date order, each copago is paid by the beneficiary until the running total reaches the deductible; the rest is covered by the insurer. For every row, `beneficiary + insurer = copago` |
| 7 | **Crossing date** | Date of the prestación where the running total reaches the deductible. Empty if it's never reached |

Worked example (seed, beneficiary 1): cotización 5.0 UF, rule 1 (29, 122 UF) → `min(145, 122) = 122 UF` × UF(2025-02-28) = 4,712,952 CLP.

Errors, never silent skips: no rule in force on the window start, or no UF value for the conversion date.
No qualifying copagos means "nothing to calculate", not an error.

---

## 5. Versioning and recalculation

**Results are never overwritten.** A new result that differs from the current one is saved as version N+1, and the previous version is marked `vigente = 0` but kept.

The reason is decided by comparing the new result with the current one, checking in this order:

| Reason | When |
|---|---|
| `inicial` | No previous result |
| `sin_cambios` | Identical result. Nothing is saved |
| `ventana_desplazada` | The window start moved (an earlier copago arrived) |
| `cambio_normativo` | Same window, different rule version |
| `correccion` | Same window and rule, different deductible (a UF value or cotización was fixed) |
| `datos_tardios` | Same window, rule and deductible, but the prestaciones changed |

Late data is always calculated with the rule of **its period**, not today's rule (spec §6).

**Rule-set validation** (spec §8.1), run every time a rule is loaded. For each code:
- no version ends before it starts
- no overlaps and no gaps between consecutive versions
- exactly one open version (`valid_to` empty)

Rules only get into the database through `cargar-regla-ges`. Don't edit the rule tables by hand (D10).

**Data warnings** (spec §8.3), run on every calculation. They're stored with the result and printed as `WARNING`, but they don't block the calculation:
- negative amount, or copago > arancel
- GES copago above 20% of the arancel
- GES code with no rules, or no rule in force on the prestación date

---

## 6. Excel ingestion

### Formats

Headers are matched ignoring case, accents and extra spaces. Column order doesn't matter.

| Field | Format A (clinic) | Format B (insurer) | Required |
|---|---|---|---|
| beneficiary id | `ID Beneficiario` | `beneficiario` | yes |
| service date | `Fecha Prestación` | `fecha` | yes |
| GES code | `Código GES` | `problema_ges` | no (empty = not GES) |
| in GES network | `Red GES` | `en_red_ges` | no (empty = yes) |
| in CAEC network | `Red CAEC` | `en_red_caec` | yes |
| arancel | `Arancel` | `monto_arancel` | yes |
| copago | `Copago` | `monto_copago` | yes |

A new layout is a new entry in `FORMATOS` in `src/motor.py`. No other code changes.

### Accepted values

| Type | Accepted |
|---|---|
| Dates | Excel date cells, `YYYY-MM-DD`, `DD-MM-YYYY`, `DD/MM/YYYY` |
| Amounts (CLP) | `1234567`, `1.234.567`, `$ 1.234.567`, `1234567,00`. Whole pesos, not negative |
| Yes/no | `S`/`N`, `Si`/`No`, `Y`/`Yes`, `1`/`0`, `true`/`false`, `X` |
| GES code | trimmed and uppercased |

### What gets rejected

- **Whole file:** not a readable `.xlsx`, empty, or a required column missing (usually the wrong `--formato`).
- **Single row:** a value that can't be read, a missing required value, a beneficiary that doesn't exist, or a duplicate of a stored prestación (same beneficiary, date, code, networks and amounts, D12).

Rejected rows are listed with their Excel row number and reason, and the other rows load. Blank rows are skipped. The run log records the file's SHA-256.

> Two genuinely identical copagos on the same day are caught as duplicates and need a manual check.

---

## 7. Run log and reproducibility

- Every writing command gets a row in `ejecuciones` before it runs (`en_curso`), so a crash still leaves a trace. Results, rules and ingested prestaciones point back to the run that created them.
- Each result stores `version_codigo` (the git commit, with `-dirty` if there were uncommitted changes) and `conocido_hasta`. Together they say which code and which data produced it.
- To reproduce an old result: `git checkout <tag>`, recreate the venv, rebuild the database and re-run.
- `--ahora` sets the business time stored on results. The run log always uses the real clock.

---

## 8. Seed data

`init` loads synthetic data from `sql/seed.sql`:

- **GES01 rules:** 2025 (29 cotizaciones, 122 UF, closed 2025-12-31) and 2026 (130 UF, open).
- **UF values:** month-end UF values from 2024-12 to 2026-12.
- **Beneficiary 1:** cotización 5.0 UF, GES prestaciones including one late arrival.
- **Beneficiary 2:** cotización 3.5 UF, referred to CAEC on 2025-04-01. Only CAEC data, so under GES it shows "nothing to calculate".

---

## 9. Not supported

- CAEC calculation (plan phase 2).
- GES multi-event deductible (43 cotizaciones / 181 UF, D7).
- Period close: every recalculation creates a new version (D9).
- Loading rules from Excel (S14, optional).
- Deciding refunds or charges after a recalculation. The engine only shows the difference.

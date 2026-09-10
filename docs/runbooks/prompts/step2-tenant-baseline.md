TASK: Give each tenant its own ensemble baseline, derived from the cold-start bootstrap
and persisted in the existing `model_baseline` table. Per ADR-008.

Read `docs/architecture/ADR-008-tenant-baseline-provisioning.md` first.

## The problem

`EnsembleAnomalyDetector` is always fitted against `build_default_baseline()` —
**10 synthetically generated sequences** (`src/api/services/comprehensive_pipeline.py:362`,
called from `_prepare_baseline` at :505). `run_batch` calls `_prepare_baseline(None)`
(:588), so on the batch path every tenant on the platform is scored against the same
fabricated notion of "normal".

For anomaly detection that is close to meaningless. It is also a plausible cause of a
measured pathology: `MultivariateMahalanobisDetector` fires on **38.3%** of windows in a
healthy July fixture but only **9.5%** in a fixture containing a real week-long sensor
fault. It is inverted — it finds healthy data more surprising than broken data. Detectors
fitted to a fictional normal will do that.

## The infrastructure already exists and is unused

- **`model_baseline` table** — `src/db/models.py:156`. Columns: `id`, `customer_id`,
  `greenhouse_type`, `baseline_json`, `created_at`. It is only *imported* by
  `src/models/model_registry.py`; nothing reads or writes it. **No migration needed.**
- **The cold-start bootstrap already produces the right input** — per-tenant,
  physics-filtered clean rows at `bootstrapped_clean_{tenant_id}.csv`
  (`src/api/routes/bootstrap.py:95`, `src/config.py:890` `BOOTSTRAP_CLEAN_PATH`).
  Read `src/models/bootstrap.py` and `src/api/services/cold_start_training.py` to see how
  that file is produced and consumed today.
  Note: as of 2026-09-09 the bootstrap loader retains 100% of input rows (it previously
  reservoir-sampled ~8.6% with an unseeded RNG), so its output is trustworthy for the
  first time.

## What to implement

1. **Derive a baseline** from a tenant's bootstrap clean output: sample N sequences of
   shape `(SEQ_LEN, INPUT_DIM)` from the clean rows, normalised the same way the scoring
   path normalises (reuse `src/models/data_utils.py` — do NOT write a second
   normalisation). N should be a named constant in `src/config.py`, not a literal.
   Sample across the whole time span, not the first N rows — a baseline drawn only from
   January is not this greenhouse's normal.

2. **Persist it** to `model_baseline` keyed by `customer_id`, with `created_at` giving
   versioning. Store the array in `baseline_json`. Guard against non-finite floats before
   serialising — SQLite tolerates NaN in JSON, production PostgreSQL returns a 500. There
   is an established pattern for this guard; find it (`src/live/events.py` sanitisation,
   `src/api/services/score_transform.py`) and follow it rather than inventing one.

3. **Read it** in `_prepare_baseline`: when a stored baseline exists for the tenant, use
   it; otherwise fall back to `build_default_baseline()` exactly as today. Tenants without
   a stored baseline must behave identically to now — this is a strict improvement, never
   a regression. `_prepare_baseline` currently takes no tenant identity; threading it in
   is part of the task. Keep the parameter count at or below 3 (use an object if needed).

4. **Read the newest** baseline per tenant when several rows exist. Do not delete old
   rows — `created_at` versioning is the point, and a retrain should be able to supersede
   without destroying history.

5. Log at INFO which baseline was used (stored-for-tenant vs synthetic default) and its
   `created_at`. When an ensemble-derived detector misbehaves later, the first question
   will be "what was it fitted to".

## Explicitly out of scope

- Do NOT recalibrate any detector threshold. That is Step 3 and depends on this landing
  first. Changing both at once makes it impossible to attribute an improvement.
- Do NOT fit a baseline from the uploaded file being scored. ADR-008 rejects this: the
  baseline would absorb the fault being hunted. The December fixture is 163.83 h frozen
  at exactly 100.0% RH — "normal" would include a dead sensor.
- Do NOT key baselines by `greenhouse_type`. The column exists but ADR-008 rejects it.
- Do NOT add a migration. The table already exists.

## Acceptance

Test file: `tests/models/test_tenant_baseline.py`

  - a baseline derived from a tenant's clean rows has shape `(N, SEQ_LEN, INPUT_DIM)`
  - stored then read back round-trips to the same array within float tolerance
  - `_prepare_baseline` for a tenant WITH a stored baseline returns the stored one
  - `_prepare_baseline` for a tenant WITHOUT one returns the synthetic default, and the
    result is identical to today's behaviour (pin this — it is the no-regression test)
  - two tenants with different stored baselines get different baselines (no cross-tenant
    leakage — this repo is multi-tenant and every query must filter by tenant)
  - when several baselines exist for one tenant, the newest by `created_at` is used
  - non-finite values are rejected or sanitised before serialisation, not written

Keep tests fast: synthesise clean rows, do not read the 12 MB export in a unit test.

## Regression

Baseline is **719 passed, 1 failed, 8 skipped, 1 xfailed**. The expected failure is
`tests/api/test_023_migration.py::TestNonFiniteGuard::test_nan_in_live_row_becomes_null_with_unknown_scale`
(SQLite converts NaN to NULL on insert). Leave it.

    venv/bin/python -m pytest tests/ -q --timeout=600 --ignore=tests/load

Any other failure is yours. In particular the stuck-fault and episode suites must be
untouched — confirm the measured stuck latency is still 6.00 h and the frozen durations
are still 34.83 / 22.08 / 47.92 / 163.83 h:

    venv/bin/python -m pytest tests/api/test_stuck_fault_upload.py tests/api/test_episode_grouping.py -q --timeout=300

When complete:
mkdir -p tasks/done
echo '{"agent":"ml-engineer","status":"done","output":"src/models/tenant_baseline.py","tests_passed":true,"baseline_sequences":"REPLACE","fallback_verified":"REPLACE"}' > tasks/done/ml-engineer.json
On failure:
echo '{"agent":"ml-engineer","status":"failed","reason":"brief"}' > tasks/done/ml-engineer.json

# Sprint B — Coverage Report

**Date:** 2026-06-21  
**Run by:** qa-engineer  
**Tool:** `./venv/bin/pytest tests/ --cov=src --cov-fail-under=80 -q`

---

## Summary

| Metric | Value |
|--------|-------|
| Total coverage | **88.41%** |
| Coverage floor | 80% |
| Floor met? | ✅ Yes |
| Tests passed | 1 206 |
| Tests failed | 4 (all pre-existing) |
| Tests skipped | 12 |
| xfailed | 1 |

---

## Sprint B — Changed Files Coverage

| File | Stmts | Miss | Cover | Notes |
|------|-------|------|-------|-------|
| `src/models/gas_anomaly_detector.py` | 85 | 5 | **93.04%** | EWMA removed; pure z-score + IQR |
| `src/models/fault_classifier.py` | 28 | 0 | **100.00%** | `FaultType.UNKNOWN` added |
| `src/models/ensemble_detector.py` | 175 | 6 | **94.15%** | Identity cache, vectorised range check, `assert→RuntimeError` |
| `src/api/services/comprehensive_pipeline.py` | 196 | 0 | **99.58%** | Savepoint in `_save_trackers` |
| `src/db/learning_state_store.py` | 30 | 0 | **100.00%** | `.with_for_update()` added |
| `src/api/schemas/incidents.py` | 88 | 4 | **92.39%** | `fault_type` field_validator |
| `src/api/schemas/gas.py` *(NEW)* | 25 | 0 | **100.00%** | Pydantic gas response models |
| `src/api/routes/gas_anomalies.py` | — | — | (via route tests) | `response_model` wired + `co2→gas_co2` alias |
| `src/vendors/csv_adapter.py` | 181 | 23 | **85.60%** | Outdoor/plant temperature fallback rules |

All Sprint B files meet or exceed the 80% floor. No new files fall below threshold.

---

## Pre-Existing Failures (excluded from Sprint B assessment)

These 4 failures existed before Sprint B and are **not** regressions:

| Test | Failure |
|------|---------|
| `tests/api/test_upload.py::test_upload_invalid_csv_format` | `assert 200 == ...` |
| `tests/api/test_upload.py::test_upload_nan_values` | `assert 200 == 422` |
| `tests/api/test_upload.py::test_upload_out_of_range` | `assert 200 == 422` |
| `tests/integration/test_end_to_end.py::test_error_recovery` | `assert 200 == ...` |

No new test failures introduced by Sprint B changes.

---

## Notable Coverage Gaps (below 90% in src/)

| File | Cover | Uncovered lines |
|------|-------|-----------------|
| `src/api/routes/incidents.py` | 66.36% | 207–212, 225–233, 244–273, 289–299 |
| `src/monitoring/prometheus_metrics.py` | 71.43% | 36–39, 47, 57 |
| `src/vendors/priva_client.py` | 80.39% | 85, 87, 91, 93, 113–114, 138, 146, 157–158, 175–178 |

These gaps pre-date Sprint B. `incidents.py` routes (bulk operations, archive) and `prometheus_metrics.py` registry teardown paths are the main candidates for future coverage investment.

---

## Bugs Found (not fixed — QA reports only)

1. **`csv_adapter.py` lines 94–103** — outdoor/plant temperature fallback branches partially untested; edge case where both `outdoor_temp` and `plant_temp` columns are absent simultaneously may silently return `None` without logging.
2. **`gas_anomaly_detector.py` lines 132–135** — the IQR branch for zero-IQR guard (`iqr == 0`) falls through to z-score without emitting a warning; downstream callers receive no signal that IQR was degenerate.
3. **`ensemble_detector.py` lines 80, 143** — identity-cache invalidation path and the `RuntimeError` branch on invalid sensor range are reachable but not covered by current tests; the `assert→RuntimeError` migration is functionally correct but the error message is not asserted in any test.

---

## Alembic Migration

`alembic/versions/011_add_fault_type_constraint.py` — adds a `CHECK` constraint for the `fault_type` column. No unit test coverage for migration scripts (by convention); constraint correctness validated indirectly via `incidents` schema tests.

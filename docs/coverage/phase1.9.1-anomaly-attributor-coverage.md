# Coverage Summary — Week 1 Phase 1.1 (Anomaly Attributor)

**Date:** 2026-06-12
**Command:** `./venv/bin/python -m pytest tests/ --cov=src --cov-fail-under=90`
**Result:** 534 passed, 1 skipped, 1 xfailed — **TOTAL coverage 93.83%** (gate: 90%) ✅

## Phase 1.1 Module (new for Anomaly Attribution)

| Module | Stmts | Miss | Branch | BrPart | Coverage |
|---|---|---|---|---|---|
| `src/models/anomaly_attributor.py` | 93 | 0 | 22 | 0 | **100.00%** |

`src/config.py` additions (`ATTRIBUTION_AFFECTED_SENSOR_THRESHOLD`,
`ATTRIBUTION_CONFIDENCE_SCALE`, `EXPLANATION_DEVIATION_THRESHOLD`) remain at
100.00%.

## Test File (Phase 1.1 QA)

| File | Tests | Focus |
|---|---|---|
| `tests/models/test_anomaly_attributor.py` | 36 | Spec smoke tests (attribution sums to 1, affected-sensor threshold, confidence range, batch, explanation, baseline), plus: baseline-not-set guard, zero-contribution uniform fallback, single-dominant-sensor attribution, deterministic repeat calls, custom sensor names, device handling, `AttributionStrength` gini/entropy identities and edge cases (empty, all-zero, one-hot distributions) |

## Notes

- Spec called for 20+ tests; delivered 36, all passing.
- Module-only run requires overriding the global gate from `pytest.ini`:
  `./venv/bin/python -m pytest tests/models/test_anomaly_attributor.py --cov=src.models.anomaly_attributor --cov-fail-under=0`
- QA agent completed the test work but hit the CLI spend limit before writing
  this summary; numbers above were re-verified by the orchestrator on
  2026-06-12.

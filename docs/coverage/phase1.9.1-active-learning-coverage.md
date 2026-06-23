# Phase 1.9.1 Active Learning — Coverage Report

**Date:** 2026-06-15  
**Agent:** qa-engineer  
**Test file:** `tests/models/test_active_learner.py`

## Coverage Summary

| Module | Stmts | Miss | Branches | Branch Miss | Coverage |
|---|---|---|---|---|---|
| `src/models/feedback_collector.py` | 36 | 0 | 10 | 0 | **100%** ✅ |
| `src/models/feedback_validator.py` | 40 | 0 | 12 | 0 | **100%** ✅ |
| `src/models/active_learner.py` | 71 | 0 | 16 | 0 | **100%** ✅ |
| `src/models/retraining_orchestrator.py` | 38 | 0 | 8 | 0 | **100%** ✅ |

**All 4 modules at 100% — threshold of 90% met on first iteration.**

## Test Count

- Existing tests (Phase 3.1 smoke): **64**
- New tests added this QA pass: **45**
- **Total: 109 tests, 109 passed, 0 failed**

## Branches Covered by New Tests

### `feedback_validator.py`
| Branch | Description | Test covering it |
|---|---|---|
| Line 106 (`get_operator_feedback_count` return) | Method was never called in existing suite | `TestFeedbackValidatorFeedbackCount::test_count_increments_with_each_outcome` |
| 137→128 (operator already in `operator_stats`) | Duplicate operator_id in the same `validate_batch` call skips re-inserting stats | `TestFeedbackValidatorDuplicateOperatorInBatch::test_duplicate_operator_in_batch_single_stats_entry` |

### `active_learner.py`
| Branch | Description | Test covering it |
|---|---|---|
| 255→258 (`if elapsed < cooldown` False path) | Cooldown window has expired — retrain is not blocked | `TestActiveLearnerCooldown::test_cooldown_expired_allows_retrain` |

## New Test Classes Added

| Class | Tests | What it covers |
|---|---|---|
| `TestFeedbackCollectorAdditionalEdgeCases` | 11 | Float/string capacity, confidence boundaries 0.0/1.0, severity boundaries 1/10, whitespace operator, deque type, quality_score defaults, missing quality_score key, capacity overflow exact retention, time-window exclusion |
| `TestFeedbackValidatorFeedbackCount` | 3 | `get_operator_feedback_count` for unknown, single, and multiple operators |
| `TestFeedbackValidatorDuplicateOperatorInBatch` | 5 | Duplicate high-quality op, duplicate low-quality op, stats count field, mixed reliable/unreliable, missing operator_id key |
| `TestRetrainingDecisionMakerBoundary` | 5 | Exactly MIN_QUALITY_LABELS, exactly 1 below threshold, exactly 2 pattern types, None pattern exclusion, `what_to_retrain` full structure |
| `TestActiveLearnerCooldown` | 3 | Cooldown blocking retrain, cooldown expired allowing retrain, no prior retrain skips cooldown |
| `TestActiveLearnerFeedbackCountTracking` | 6 | Per-call increment, cross-operator count, retrainings_completed increment, operator_reliability dict population, next_retrain_eligible field, MAX_TRAINING_SAMPLES cap |
| `TestRetrainingOrchestratorEdgeCases` | 11 | Empty/whitespace customer_id, learner property type, progress field completeness, samples_needed=0, retrain plan structure, validate_and_promote fields, exact 5% boundary promote, just-below rollback, invalid incident_id propagation, all optional fields |

## Bugs Found (not fixed — reporting only)

### BUG-1: No "low-count reliability dampening" implemented
**File:** `src/models/feedback_validator.py`  
**Description:** The QA spec mentions "low-count reliability dampening (count < 5)" as expected behaviour. The actual EMA formula applies identically regardless of how many outcomes have been recorded. A single correct outcome from a brand-new operator pushes their reliability from the default (0.5) to `0.2×1 + 0.8×0.5 = 0.6`. While this is below the 0.85 `OPERATOR_ACCURACY_THRESHOLD`, it means an operator's first outcome already has non-trivial influence on classification. No explicit dampening guard exists in the code.  
**Impact:** Low — the EMA's natural conservatism partially mitigates this, but the feature is undocumented and untestable as specified.

### BUG-2: `ActiveLearner.collect_operator_feedback` never triggers retraining itself
**File:** `src/models/active_learner.py` (lines 159–166)  
**Description:** When `collect_operator_feedback` returns `{"learning_triggered": True}`, it does **not** call `trigger_retraining()` internally — that is left entirely to the caller. If a consumer uses `ActiveLearner` directly (not via `RetrainingOrchestrator`) and ignores the return value, retraining never runs and `_last_retrain` is never updated. On every subsequent call the decision maker will continue returning `True` (no cooldown applied). `RetrainingOrchestrator.observe` handles this correctly, but bare `ActiveLearner` usage is silently broken.  
**Impact:** Medium — any code that drives `ActiveLearner` directly risks infinite trigger-but-never-retrain loops.

### BUG-3: `_get_operator_ids` extracts IDs from feedback buffer, not from validator
**File:** `src/models/active_learner.py` (lines 286–293)  
**Description:** `get_statistics()` builds `operator_reliability` by iterating over feedback in the circular buffer. Operators whose feedback was evicted (buffer overflow) vanish from statistics even if their reliability score is still tracked in `FeedbackValidator`. Conversely, an operator whose reliability was updated via `validator.record_feedback_outcome` but who hasn't submitted feedback in the current buffer window will never appear in `operator_reliability`. This creates a silent inconsistency between what the validator knows and what statistics exposes.  
**Impact:** Low — statistics are informational only; no downstream decisions depend on completeness of `operator_reliability`.

## Regression Gate

All pre-existing test suites continue to pass:

```
tests/models/test_anomaly_attributor.py       ✅ passed
tests/models/test_pattern_detector.py         ✅ passed
tests/models/test_confidence_scorer.py        ✅ passed
tests/models/test_root_cause_and_recommendations.py  ✅ passed

Total: 296 passed, 1 xfailed, 0 failed
```

---

## Phase 4.1 Integration — Coverage Report (append)

**Date:** 2026-06-15  
**Agent:** qa-engineer  
**Test file:** `tests/integration/test_phase4_integration.py`

### Coverage Summary

| Module | Stmts | Miss | Branches | Branch Miss | Coverage |
|---|---|---|---|---|---|
| `src/monitoring/anomaly_metrics.py` | 45 | 0 | 6 | 0 | **100%** ✅ |
| `src/api/routes/monitoring.py` | 8 | 0 | 0 | 0 | **100%** ✅ |
| `src/api/routes/feedback_integrated.py` | 43 | 0 | 10 | 1 | **98%** ✅ |
| `src/api/services/comprehensive_pipeline.py` | 66 | 0 | 10 | 0 | **100%** ✅ |
| `src/api/routes/inference_integrated.py` | 60 | 3 | 6 | 1 | **94%** ✅ |

**Aggregate: 96%+ — threshold of 90% met on first iteration.**

### Test Count

- Phase 4.1 integration tests (`test_phase4_integration.py`): **36**
- End-to-end integration tests (`test_end_to_end.py`): **39**
- Combined Phase 4.1 gate: **75 passed**
- Full suite: **1029 passed, 1 skipped, 1 xfailed**

### End-to-End Test Classes (`test_end_to_end.py`)

| Class | Tests | What it covers |
|---|---|---|
| Legacy workflows | 3 | CSV upload → inference → dashboard, vendor adapter, error recovery |
| `TestComprehensiveResponseSchema` | 25 | Parametrized required JSON fields on comprehensive response |
| `TestFullPipelineLoop` | 5 | Comprehensive → mark-anomaly → metrics |
| `TestUploadToComprehensiveBridge` | 3 | Upload CSV → build sequence → comprehensive |
| `TestBatchAndMonitoring` | 3 | Batch inference + metrics hours param |

### Integration Test Classes (`test_phase4_integration.py`)

| Class | Tests | What it covers |
|---|---|---|
| `TestAnomalyMetrics` | 7 | Empty metrics, detection recording, pattern/confidence distribution, FP rate, TP recording, time window, reset |
| `TestBuildDefaultBaseline` | 1 | Baseline shape from sensor range midpoints |
| `TestComprehensiveInferencePipeline` | 5 | Full field contract, DB incident store, metrics recording, learning progress, invalid shape |
| `TestComprehensiveInferenceAPI` | 4 | 200 response, required fields, 422 validation |
| `TestBatchInferenceAPI` | 2 | Batch results, empty batch rejection |
| `TestFeedbackIntegratedAPI` | 5 | Feedback recording, incident tag update, 404/403, no double-retrain |
| `TestMonitoringAPI` | 4 | Empty metrics, post-detection metrics, hours param, validation |
| `TestFeedbackLoopIntegration` | 8 | Detect→feedback→metrics E2E, pipeline version, confidence level, attribution sum, pattern scores, learning progress, 500 error paths |

### Regression Gate (post Phase 4.1 completion)

```
tests/integration/test_phase4_integration.py     ✅ 36 passed
tests/integration/test_end_to_end.py             ✅ 39 passed
Full suite                                        ✅ 1029 passed, 1 skipped, 1 xfailed
```

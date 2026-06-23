# Phase 1.9.1 Tech-Lead Audit

**Date:** 2026-06-15
**Auditor:** tech-lead
**Scope:** Phases 1.1, 1.2, 2.1, 2.2, 3.1, 4.1

## Executive Summary

Phase 1.9.1 delivered a complete ML inference pipeline from detection through recommendations, with active learning and monitoring. Code quality is high: 1029 tests pass, all modules have 96-100% coverage, and six consecutive code reviews passed with notes. The two most important gaps are (1) cross-subsystem isolation -- inference, incident storage, and learning operate as independent silos with no automatic wiring between them, and (2) in-memory state (metrics singleton, pipeline cache, orchestrator registry) that is lost on restart and unsafe under multi-worker deployment.

---

## 1. Missing Implementation

### P0 -- Blocking Production

| # | What | Where | Why it matters | Effort |
|---|------|-------|----------------|--------|
| P0-1 | `AnomalyMetrics` is an in-process singleton with no persistence | `src/monitoring/anomaly_metrics.py:14-16, 111-116` | All detection volume, FP rate, and confidence distribution data is lost on process restart. A production deployment that restarts (crash, deploy, scaling) starts with zero metrics history. | M |
| P0-2 | `CONFIDENCE_LEVEL_BOUNDS` has no startup validation for descending sort order | `src/config.py:381-386`, consumed at `src/models/confidence_scorer.py:231-233` | `confidence_level()` iterates bounds top-down assuming descending order. If a future edit reorders the list (or a config override mechanism loads them out of order), confidence labels silently break -- every score would match the first entry. No runtime assertion guards this invariant. | S |
| P0-3 | `_pipeline_cache` in inference_integrated.py never evicts entries | `src/api/routes/inference_integrated.py:34` | Each distinct checkpoint path creates a new cached pipeline (with model, ensemble, attributor instances). In a multi-tenant deployment with per-customer model checkpoints, memory grows without bound. No LRU, TTL, or max-size eviction. | M |

### P1 -- Important Before Next Phase

| # | What | Where | Why it matters | Effort |
|---|------|-------|----------------|--------|
| P1-1 | Cross-subsystem integration is manual | `src/api/services/comprehensive_pipeline.py` stores incidents but does not call `/api/learning/observe`; feedback routes do not propagate to the online learner; inference does not auto-trigger incident resolution workflows | Each subsystem (inference, incidents, feedback, online learning) operates in isolation. An operator must manually coordinate between endpoints. This is the single largest functional gap for a production workflow. | L |
| P1-2 | `sensor_malfunction` key in `RECOMMENDED_ACTIONS` is unreachable | `src/models/recommendation_engine.py:102-108` | No `DIAGNOSIS_RULES` entry in `root_cause_analyzer.py` produces `"sensor_malfunction"` as a cause. The 5 actions and downtime estimate (line 161) are dead code. Either add a matching diagnosis rule or remove the orphan. | S |
| P1-3 | `feedback` property on `FeedbackCollector` exposes mutable deque | `src/models/feedback_collector.py:133-136` | Despite the "Read-only access" docstring, returns the actual `_feedback` deque. Any caller can `append()`, `clear()`, or mutate items, bypassing validation. Callers in `active_learner.py` (lines 311, 337) iterate it, which works safely, but the contract is misleading. | S |
| P1-4 | No Prometheus/OpenTelemetry export for `AnomalyMetrics` | `src/monitoring/anomaly_metrics.py` | Metrics are only accessible via the `/api/monitoring/metrics` endpoint. No scrape endpoint for Prometheus, no push to Datadog/OTLP. Production observability requires one of these. | M |
| P1-5 | `learning_progress` returns `null` for customers without prior feedback -- not documented in OpenAPI | `src/api/services/comprehensive_pipeline.py:229-233`, `src/api/routes/inference_integrated.py:60-63` | The comprehensive inference response includes `learning_progress: null` when the customer has no orchestrator. The OpenAPI schema allows this (Optional), but there is no `description` field explaining why it is null or how to start getting values. | S |
| P1-6 | `_gather_evidence` substring fallback is now dead code after BUG-006 fix | `src/models/root_cause_analyzer.py:380-405` | BUG-006 added `_INDICATOR_EVIDENCE` with all 18 indicator keys (lines 49-68). The `_gather_evidence` method checks this dict first (line 376), so the entire substring-matching fallback (lines 380-405) is now unreachable for any indicator defined in `DIAGNOSIS_RULES`. The fallback code should be removed or converted to a safety net with a log warning. | S |

### P2 -- Nice to Have

| # | What | Where | Why it matters | Effort |
|---|------|-------|----------------|--------|
| P2-1 | No automated load test suite | Noted in `docs/notes/integration.md` (NOTE-INT-004) | Manual benchmarks exist in the deployment checklist but no repeatable automated performance test. Important before scaling to multi-tenant production. | M |
| P2-2 | pgvector path has zero CI tests | `.github/workflows/postgres-tests.yml` exists but all existing tests use SQLite | The ivfflat index, vector similarity search, and pgvector-specific incident queries are untested in CI. Only manual verification. | M |
| P2-3 | No A/B testing framework for model promotion | Active learning promotes/rolls back based on a single `improvement_pct` comparison | No shadow-mode, canary, or A/B infrastructure to validate promoted models on live traffic before full cutover. | L |
| P2-4 | Dashboard widgets for confidence breakdown, root cause details, and recommendation cards were deferred | Noted in handover | Streamlit pages exist for metrics and anomalies but do not surface the full pipeline output (confidence signals, alternative causes, technician requirements). | M |
| P2-5 | `GET /api/incidents/similar` passes embedding as 8 repeated query params | `docs/notes/incident-memory.md` (NOTE-IM-005) | URL-unfriendly if `LATENT_DIM` grows. Should be a POST with JSON body. | S |
| P2-6 | ADR for deterministic `decode(mu)` attribution scoring not yet written | `docs/notes/anomaly-attributor.md` (NOTE-AA-001) | The deterministic reconstruction path diverges from the stochastic forward path used by the detector. Thresholds calibrated on one do not transfer to the other. This design decision should be formally documented before attribution becomes an API contract consumed by external systems. | S |
| P2-7 | Dead schema columns in incidents table | `docs/notes/incident-memory.md` (NOTE-IM-004) | `effectiveness_score`, `time_to_detect`, `reported_at`, `crop_damage_estimated`, `root_cause_confidence`, `root_cause_details` columns exist in the schema but have no write codepaths. Reserved for future phases but add schema maintenance burden. | S |
| P2-8 | `incident_actions` table has no `tenant_id` column | `docs/notes/incident-memory.md` (NOTE-IM-003) | Tenant isolation is transitive via FK to incidents. Direct action queries in future would require a join through incidents. | S |

---

## 2. Improvement Plan

Priority order with dependencies noted.

### Sprint 1 (immediate -- before staging deploy)

1. **Add startup validation for `CONFIDENCE_LEVEL_BOUNDS` sort order** (P0-2)
   - Add a module-level check in `config.py` or a constructor-time assertion in `ConfidenceScorer.__init__`
   - No dependencies
   - Effort: 30 min

2. **Add LRU eviction to `_pipeline_cache`** (P0-3)
   - Use `functools.lru_cache` or a simple max-size dict with FIFO eviction
   - No dependencies
   - Effort: 1-2 hours

3. **Fix `feedback` property to return immutable view** (P1-3)
   - Return `tuple(self._feedback)` instead of the raw deque
   - Verify callers in `active_learner.py` lines 311, 337 still work with tuple iteration
   - Effort: 30 min

4. **Remove or wire `sensor_malfunction` actions** (P1-2)
   - Recommendation: add a `DIAGNOSIS_RULES` entry for a sensor-malfunction pattern, or remove the orphan key and its downtime estimate
   - Effort: 1 hour

5. **Document `learning_progress: null` in OpenAPI schema** (P1-5)
   - Add `description` field to `LearningProgressSummary` in the Pydantic schema
   - Effort: 15 min

6. **Clean up dead `_gather_evidence` fallback code** (P1-6)
   - Remove or simplify the substring-matching fallback in `_gather_evidence` since `_INDICATOR_EVIDENCE` now covers all indicators
   - Effort: 30 min

### Sprint 2 (before production)

7. **Persist `AnomalyMetrics` to database or external store** (P0-1)
   - Options: periodic flush to a `metrics_snapshots` table, or direct Prometheus client integration (P1-4)
   - Dependency: decision on monitoring stack (Prometheus vs custom DB)
   - Effort: 1-2 days

8. **Add Prometheus scrape endpoint** (P1-4)
   - Use `prometheus-client` library, expose `/metrics` with gauges/counters
   - Depends on: P0-1 decision
   - Effort: 1 day

9. **Cross-subsystem wiring** (P1-1)
   - Phase A: comprehensive pipeline auto-calls online learner `observe` after detection
   - Phase B: feedback_integrated auto-propagates to online learner
   - Phase C: incident resolution triggers follow-up actions
   - Depends on: stable API contracts (already in place)
   - Effort: 3-5 days

### Sprint 3 (hardening)

10. **pgvector CI tests** (P2-2) -- requires PostgreSQL service in CI
11. **Automated load test suite** (P2-1) -- using locust or k6
12. **Write ADR-002 for deterministic attribution** (P2-6)

---

## 3. Refactoring Suggestions

### `src/models/root_cause_analyzer.py` -- 437 lines (cap: 300)

- **Line count:** 437 lines including the `_INDICATOR_EVIDENCE` dict (lines 49-68) and `DIAGNOSIS_RULES` (lines 88-186). Exceeds the 300-line cap by 137 lines.
- **Suggested fix:** Extract `DIAGNOSIS_RULES`, `_INDICATOR_EVIDENCE`, `_HIGH_SEVERITY_PATTERNS`, and `_MEDIUM_SEVERITY_PATTERNS` into a `src/models/diagnosis_rules.py` data module. The analyzer imports and consumes them. This brings the analyzer to ~270 lines (logic only) and the rules module to ~170 lines (data only). Both under cap.
- **`_sensors_match` substring matching** (line 364): The standard-case branch `sensor.lower() in rule_sensors.lower()` uses substring matching. Safe with current 4 sensor names but fragile. A sensor named `"co"` would match `"co2"`. Suggested fix: split `rule_sensors` on `_` and check for exact sensor name membership.

### `src/models/anomaly_attributor.py` -- 361 lines (cap: 300)

- **Line count:** 361 lines including `AttributionStrength` class (lines 321-361).
- **Suggested fix:** Move `AttributionStrength` to `src/models/attribution_strength.py`. The attributor drops to ~320 lines. If further trimming is needed, the lengthy return-value docstring in `attribute_sequence` (lines 140-157) could be shortened.

### `src/models/confidence_scorer.py` -- typing and DRY

- **Line 71:** `SIGNAL_NAMES: tuple` should be `Tuple[str, ...]` for correct static analysis of element types.
- **Lines 192-198:** `assess()` re-computes the weighted sum instead of calling `self.score()`. Minor DRY violation -- acceptable for avoiding double computation of signals, but should have a comment explaining the intentional duplication.

### `src/monitoring/anomaly_metrics.py` -- unbounded list growth

- **Lines 22-24:** `detections`, `false_positives`, `true_positives` are plain lists that grow without bound. `get_metrics()` filters by time window but the underlying lists are never pruned.
- **Suggested fix:** Use a `deque` with `maxlen` based on expected max events per retention period, or add periodic pruning (e.g., drop entries older than `MAX_METRICS_RETENTION_HOURS` on each `record_detection` call).

### `src/models/pattern_scores.py` -- duplicate z-score + hardcoded divisor

- **Lines 112 and 229:** The expression `np.abs(sequence - baseline.mean) / (baseline.std + NUMERICAL_EPSILON)` appears identically in `score_spike` and `score_cascade`. Extract to a `_z_scores(sequence, baseline)` helper to prevent the two from drifting apart on epsilon or normalization handling.
- **Line 280:** `len(x) // 4` is the only hardcoded tuning parameter in the pattern scoring module. All 19 sibling constants are in `config.py`. Extract to `PATTERN_LAG_WINDOW_DIVISOR` or `PATTERN_LAG_MAX_SEQ_FRACTION`.

### `src/models/pattern_scores.py` -- frozen dataclass with mutable arrays

- **Lines 44-65:** `PatternBaseline` is `@dataclass(frozen=True)` but numpy arrays are mutable objects. A buggy consumer could do `baseline.mean[0] = 999.0` without error, silently corrupting shared state. No current code does this.
- **Suggested fix:** Set `.flags.writeable = False` on each array in `compute_baseline` before constructing the dataclass.

### `src/api/routes/inference_integrated.py` -- batch processes sequences sequentially

- **Lines 147-154:** `batch_inference` runs each sequence through `pipeline.run()` in a serial `for` loop. Each call re-creates an `AnomalyAttributor` and `PatternDetector` (`comprehensive_pipeline.py:122-128`). For batches, these objects could be created once and reused.
- **Priority:** Low -- current batch sizes are small. Flag for optimization when batch latency becomes a concern.

### `src/config.py` -- import placement and bare `dict` types

- **Lines 480-505:** `import os` and a second `from typing import Dict` appear mid-file, below all other imports. Move to the top-level import block for consistency.
- **Lines 70, 130, 139, 149, 335, 413:** `PATHS: dict`, `MODEL_CONFIG: dict`, etc. should use `Dict[str, ...]` with concrete value types for static analysis.

### `src/api/main.py` -- f-string logging

- **Lines 61, 72, 116-117, 123-126, 138-141:** Uses f-string interpolation in `logger.info()` and `logger.error()` calls. The string is evaluated even when the log level is disabled. Use %-style formatting: `logger.info("Request started: %s %s", method, path)`.

### `src/models/feedback_collector.py` -- `operator_confidence` validation note

- **Line 156:** Validates `0.0 <= operator_confidence <= 1.0` but does not explicitly check `math.isfinite()`. In practice, `float('nan')` comparison with `<=` returns `False` in Python, so NaN is rejected. However, adding explicit `isfinite()` would be consistent with the defense-in-depth pattern used in `confidence_scorer.py`, `retraining_orchestrator.py`, and `anomaly_attributor.py`.

### `src/api/services/comprehensive_pipeline.py` -- per-call object creation

- **Lines 122-128:** Each `pipeline.run()` call creates a new `AnomalyAttributor` and `PatternDetector`, including re-setting baselines. For the single-inference endpoint this is acceptable. For batch, these objects should be created once per request and reused across sequences.

### Cross-cutting: test file sizes

- `tests/models/test_confidence_scorer.py` (968 lines), `tests/models/test_pattern_detector.py` (789 lines), `tests/models/test_root_cause_and_recommendations.py` (665 lines) all exceed the 300-line cap. These are comprehensive and well-organized but could be split by test class into subdirectories (e.g., `tests/models/test_pattern_detector/test_drift.py`). This is a project-wide convention decision, not a per-module fix.

### Schema drift: `tenant_id` length mismatch

- Migration `001_initial` uses `String(100)` for `tenant_id`; ORM model uses `String(255)`. Harmless in practice (SQLite ignores length constraints; PostgreSQL would truncate at 100). Fix in the next migration to align both to `String(255)`.

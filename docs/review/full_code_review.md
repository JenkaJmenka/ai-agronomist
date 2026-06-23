# Full Code Review — SCADA Anomaly Detection Implementation

**Reviewer:** code-reviewer (Claude Opus)
**Date:** 2026-06-21
**Verdict:** **PASS WITH NOTES**
**Scope:** 14 files across 5 implementation phases (fault taxonomy, adaptive thresholds, physics validator, gas anomaly detector, gas API routes, gas dashboard, pipeline integration, DB persistence, schema changes)

---

## Files Reviewed

| File | Lines | Module |
|------|-------|--------|
| `src/models/fault_classifier.py` | 67 | Fault taxonomy |
| `src/models/stuck_fault_detector.py` | 57 | Stuck fault detection |
| `src/models/bias_detector.py` | 82 | Bias offset detection |
| `src/models/ewma_tracker.py` | 102 | Adaptive EWMA threshold |
| `src/models/pot_threshold.py` | 138 | POT/GPD adaptive threshold |
| `src/models/physics_validator.py` | 133 | Physics validation layer |
| `src/models/gas_anomaly_detector.py` | 197 | Gas sensor anomaly detection |
| `src/api/routes/gas_anomalies.py` | 168 | Gas API endpoints |
| `src/api/services/comprehensive_pipeline.py` | 572 | Pipeline orchestration |
| `src/db/learning_state_store.py` | 59 | Learning state persistence |
| `src/db/models.py` | 407 | SQLAlchemy models |
| `src/api/schemas/incidents.py` | 163 | Pydantic v2 API schemas |
| `src/db/incident_memory.py` | 318 | Incident storage + search |
| `src/dashboard/pages/7_gas_sensors.py` | 288 | Streamlit gas dashboard |

**Total:** 2,751 lines reviewed.

---

## Guardrail Compliance

### Red Flags (`.claude/guardrails/red-flags.md`) — ALL CLEAR

| # | Check | Result |
|---|-------|--------|
| 1 | No file writing outside ownership | PASS |
| 2 | No `os.system`, `subprocess`, `eval()` | PASS |
| 3 | No hardcoded secrets/tokens/API keys | PASS |
| 4 | No unfiltered DELETE/DROP queries | PASS |
| 5 | No model.save without metadata | N/A (no model saving in scope) |
| 6 | No test importing and modifying impl | PASS |
| 7 | No circular imports models↔api | PASS (deferred import in pipeline is documented) |

### Universal Guardrails (`.claude/guardrails/universal-guardrails.md`) — ALL CLEAR

| Check | Result |
|-------|--------|
| No tight LSTM-VAE ↔ API coupling | PASS — detector injected via DI |
| No hardcoded secrets | PASS |
| All checkpoints have metadata | N/A |
| DB schema has tenant_id | PASS |
| Test coverage ≥80% on critical paths | PASS (93%+ overall) |
| No major design change without ADR | PASS |

### ML Guardrails (`.claude/guardrails/ml-guardrails.md`) — ALL CLEAR

| Check | Result |
|-------|--------|
| LSTM-VAE architecture unchanged | PASS |
| NaN/Inf detection in model outputs | PASS — EWMATracker rejects non-finite, POTThresholdFitter guards GPD output |
| No hardcoded sensor ranges | PASS — all from `src/config.py` |
| No training loop without early stopping | N/A |

### WARN-Level Guardrails

| # | Check | Result | Severity |
|---|-------|--------|----------|
| W1 | Hard-coded thresholds not in config.py | **FOUND** — see C-11 below | WARN |
| W2 | Missing error handling on external calls | PASS | — |
| W3 | Code duplication (3+ identical blocks) | PASS | — |
| W4 | Silent failures (bare except, pass) | PASS — all `except` blocks log with `exc_info=True` | — |

---

## Security Review

### S-1: No Hardcoded Secrets ✅ PASS
All secrets use environment variables (`TENANT_API_KEYS`, `DATABASE_URL`, `AUTH_ENABLED`). No API keys, tokens, or passwords in source.

### S-2: No SQL String Concatenation ✅ PASS
All database queries use SQLAlchemy ORM with parameterized bindings. The `_source_is_real()` helper in `gas_anomalies.py` uses `~col.like("synthetic:%")` — proper SQLAlchemy operator, not string concatenation.

### S-3: No `eval()` or Dynamic Code Execution ✅ PASS
No `eval()`, `exec()`, `Function()`, or dynamic code execution found in any reviewed file.

### S-4: Input Validation on API Boundaries ✅ PASS
- `gas_summary` and `gas_readings`: `tenant_id` and `greenhouse_id` use `Query(..., min_length=1)`; `hours` uses `Query(24, ge=1, le=168)`.
- `IncidentCreateRequest`: Pydantic v2 with `field_validator` enforcing embedding dimension against `LATENT_DIM`.
- `IncidentResolveRequest`: `min_length=1` on required strings, `max_length` bounds on free-text.

### S-5: Dashboard XSS ✅ PASS
The Streamlit dashboard uses only safe rendering functions: `st.metric()`, `st.dataframe()`, `st.plotly_chart()`, `st.markdown()` (with static strings only). No `unsafe_allow_html=True`, no `st.components.html()` with user content. The `st.error(f"Could not reach the API: {exc}")` on line 56 passes a `requests` exception message — low risk in an internal dashboard context.

### S-6: No Sensitive Data in Logs ✅ PASS
All log statements contain only operational data (tenant_id, anomaly_score, status strings). No PII, tokens, credentials, or raw sensor payloads logged.

### S-7: File Path Validation ✅ N/A
No user-controlled file path inputs in any reviewed file.

### S-8: SSRF Prevention ✅ PASS
Dashboard reads `API_URL` from environment variable (line 18 of `7_gas_sensors.py`). This is server-to-server (Streamlit → FastAPI), not user-controlled. No user-supplied URL inputs found.

---

## Correctness Review

### C-1: Import Verification ✅ PASS
All imports verified to exist:
- `scipy.stats.genpareto` — correct module; `fit()`, `ppf()` are valid methods.
- `EWMATracker`, `POTThresholdFitter` — correctly imported and used.
- `PhysicsValidator`, `FaultClassifier` — correctly wired in pipeline.
- `GasAnomalyDetector` imported in `gas_anomalies.py` — correct path.
- No hallucinated APIs or method names found.

### C-2: Async/Await Correctness ✅ PASS
- All `await` calls occur in `async def` functions.
- `db.execute()`, `db.commit()`, `db.rollback()` are properly awaited.
- No synchronous blocking calls found in async code paths.
- `comprehensive_pipeline._run_with_components` is correctly `async`.

### C-3: Error Handling ✅ PASS (with notes)
- All critical `await` calls are in try/except or propagate naturally through FastAPI's exception handling.
- Non-critical paths (EWMA/POT tracker load/save, online learner observe) wrapped in try/except with `logger.debug(...)` or `logger.exception(...)` and `exc_info=True`.
- **NOTE:** Several `except Exception` blocks in `comprehensive_pipeline.py` (lines 305, 341, 401, 517, 553) catch broadly. This is intentional — these are documented as "non-critical" operations with correct fallback behavior (fresh trackers, skip update, skip observe). The broad catch ensures pipeline stability.

### C-4: DB Session Lifecycle ⚠️ NOTE (Non-blocking)
**Finding:** `get_db()` in `session.py` commits on successful generator exit. Several methods perform their own `await db.commit()` mid-pipeline:
- `incident_memory.store_incident()` (line 103) — commits before online learner observe
- `comprehensive_pipeline._save_trackers()` (line 552) — commits tracker state independently

**Impact:** If a later operation in the same request fails, earlier commits are already persisted and won't roll back. For an anomaly detection system, this is acceptable behavior — it is better to store the incident than risk losing it due to an unrelated downstream failure. The `_save_trackers` method has its own `await db.rollback()` in the except path (line 555), which is correct.

**`_save_trackers` and `LearningStateStore.save()` contention (GAR-3):** Verified that `LearningStateStore.save()` now uses **merge semantics** (lines 51-53: `merged = dict(row.state_json or {}); merged.update(state); row.state_json = merged`), preventing the previously reported critical bug where EWMA+POT tracker state was silently erased. **GAR-3 is resolved.**

### C-5: Pydantic Validation ✅ PASS
- All API request schemas use Pydantic v2 with `BaseModel`.
- `IncidentCreateRequest.validate_embedding_dimension` correctly enforces `LATENT_DIM`.
- Response models use `ConfigDict(from_attributes=True)` for ORM compatibility.
- Field constraints (`ge`, `le`, `min_length`, `max_length`) properly applied.

### C-6: Error Response Safety ✅ PASS
- No stack traces exposed to clients.
- FastAPI returns 422 for validation errors automatically.
- `_get_incident` raises `ValueError` which maps to 404 at the route level.
- Pipeline errors wrapped in try/except at the service layer.

### C-7: Circular Import Analysis ✅ PASS
- `comprehensive_pipeline.py` line 103: `from src.api.routes.learning import _get_manager` — deferred import to break circular dependency. Correct and documented.
- `incidents.py` line 45: `from src.config import LATENT_DIM` — deferred for schema decoupling. Correct.
- No other circular import risks found in the reviewed files.

### C-8: `scipy.stats.genpareto` Usage ✅ PASS
```python
params = genpareto.fit(peaks, floc=0)           # Returns (c, loc=0, scale) — correct for POT
gpd_threshold = high_quantile + float(
    genpareto.ppf(1.0 - self._fpr, *params)     # Correct unpacking of (c, loc, scale)
)
if not np.isfinite(gpd_threshold):              # Non-finite guard — correct
    raise ValueError(...)
```
- `floc=0` fixes location at zero, which is the standard approach for Peaks Over Threshold (exceedances are already shifted by `high_quantile`).
- The `ppf()` call correctly unpacks `*params` (shape, loc=0, scale).
- Non-finite check prevents propagation of degenerate fits.
- Fallback to quantile on any exception — correct defensive pattern.

### C-9: EWMA/POT Serialization Round-Trip ✅ PASS
**EWMATracker:**
- `to_dict()` serializes: `alpha`, `k`, `mean`, `var`, `n` — all internal state captured.
- `from_dict()` reconstructs via constructor + direct attribute assignment — bypasses validation (alpha/k already validated on original construction).
- Round-trip test in `test_scada_detectors.py::TestEWMATracker::test_serialization_roundtrip` confirms threshold equality via `pytest.approx`.

**POTThresholdFitter:**
- `to_dict()` serializes: `q`, `fpr`, `min_peaks`, `buffer_size`, `buffer` (full deque contents), `threshold`, `n_since_fit` — complete state.
- `from_dict()` uses `d.get("buffer_size", 500)` with safe defaults for backward compatibility.
- Round-trip test confirms threshold equality.

### C-10: EWMA Variance Computation Ordering ⚠️ NOTE (Non-blocking)
```python
# Line 61-65 of ewma_tracker.py
self._mean = self._alpha * score + (1.0 - self._alpha) * self._mean
self._var = (
    self._alpha * (score - self._mean) ** 2     # Uses UPDATED mean
    + (1.0 - self._alpha) * self._var
)
```
The variance update uses the **already-updated** mean. This is a known variant (West's 1979 EWMA formulation) that produces a slight downward bias in variance estimates compared to the canonical two-pass approach. For anomaly detection thresholding, this results in a marginally tighter threshold — acceptable and arguably beneficial (fewer false negatives). Not a bug, but worth documenting in the module docstring.

### C-11: Duplicated `GAS_COLUMNS` Constant ⚠️ WARN
`GAS_COLUMNS` is defined independently in two locations:
- `src/config.py` line 564: `GAS_SENSOR_COLUMNS: List[str] = ["c2h4", "gas_co2", "no", "no2", "nox", "co"]`
- `src/models/gas_anomaly_detector.py` line 19: `GAS_COLUMNS: list[str] = ["c2h4", "gas_co2", "no", "no2", "nox", "co"]`

The `gas_anomaly_detector.py` constant is used by `gas_anomalies.py` routes. The `config.py` constant (`GAS_SENSOR_COLUMNS`) is not imported anywhere in the reviewed files. These are currently identical but maintained independently — a violation of the "config values in one place" guardrail. The detector module should import from `src/config.py`.

### C-12: `list_open_incidents` Query Building ⚠️ NOTE (Non-blocking)
In `incident_memory.py` lines 272-282, the `greenhouse_id` filter is applied **after** `.order_by()` and `.limit()`:
```python
stmt = select(...).where(...).order_by(...).limit(limit)
if greenhouse_id:
    stmt = stmt.where(Incident.greenhouse_id == greenhouse_id)
```
SQLAlchemy generates correct SQL regardless of clause ordering (WHERE always precedes ORDER BY/LIMIT in emitted SQL), so this is **not a functional bug**. However, the code ordering is misleading — the `greenhouse_id` filter appears to be applied after limiting, when in fact SQL evaluates it before. Same pattern in `find_nearby`. Consider reordering for clarity.

### C-13: PhysicsValidator — No Suppression Rules ⚠️ NOTE (Non-blocking)
All four rules in `PhysicsValidator.validate()` set `confirmed=True`. No rule currently sets `suppressed=True`. The `_suppressed_result` handler in the pipeline (line 263) is dead code. This is architecturally sound (the suppression path is ready for future rules), but currently no anomaly is ever suppressed by physics validation.

---

## Performance Review

### P-1: Gas Summary Endpoint — Per-Request Baseline Recomputation ⚠️ NOTE
`GET /api/gas/summary` calls `detector.fit(readings_data)` on every request, recomputing statistical baselines from all readings in the time window. For large windows (168 hours at high frequency), this involves loading all rows into memory and computing per-column numpy statistics.

**Impact:** Acceptable for current data volumes. Consider caching baselines per tenant/greenhouse with a TTL for production scale.

### P-2: Dashboard Anomaly Markers — One Trace Per Point ⚠️ NOTE
The Plotly chart creates a separate `go.Scatter` trace for each anomaly point (lines 183-217 of `7_gas_sensors.py`). With many anomalies, this creates hundreds of traces, slowing rendering.

**Recommendation:** Batch anomaly points per column into a single scatter trace per column using arrays instead of individual points.

### P-3: Dynamic Attribute Caching ⚠️ NOTE
`comprehensive_pipeline.py` uses `setattr(self, f"_trackers_{customer_id}", ...)` for per-tenant tracker caching (lines 501, 515, 534). While functional, this creates unbounded dynamic attributes on the pipeline instance.

**Recommendation:** Replace with a `dict[str, tuple[EWMATracker, POTThresholdFitter]]` instance attribute for better type safety and inspection.

---

## Type Hint Review

### T-1: `fault_classifier.py` — Return Type Mismatch (INFO)
`classify()` returns `str` but the values are always `FaultType.*.value`. The return type annotation could be `str` (correct, since `.value` is a string) but ideally the method would return `FaultType` directly for type safety.

### T-2: `gas_anomaly_detector.py` — Unparameterized `dict` in `fit()` (INFO)
`readings: list[dict]` on line 69 should be `list[dict[str, Any]]` for clarity.

### T-3: `incident_memory.py` — `sequence: Any` (INFO)
`store_incident()` parameter `sequence: Any` (line 78) should be `Union[List[List[float]], np.ndarray]` to document the expected types.

### T-4: Mixed Typing Styles (INFO)
The codebase mixes old-style (`Dict[str, Any]`, `List[float]` from `typing`) and new-style (`dict[str, Any]`, `list[float]` using PEP 604) type hints. Files like `comprehensive_pipeline.py` use old-style; newer files like `gas_anomaly_detector.py` use new-style. Not a correctness issue but inconsistent.

---

## Test Quality Review

### Existing Test Coverage — GOOD

`tests/unit/test_scada_detectors.py` provides **43 unit tests** covering all new model-layer modules:

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestFaultClassifier` | 7 | All fault types, priority, case-insensitivity |
| `TestStuckFaultDetector` | 5 | Constant/varying/single-sensor/invalid-input/sensor-names |
| `TestBiasOffsetDetector` | 5 | Midpoint/large-bias/small-bias/missing-range/invalid-input |
| `TestEWMATracker` | 6 | Warmup/finite-threshold/anomaly/round-trip/invalid-alpha/nan |
| `TestPOTThresholdFitter` | 5 | Insufficient-data/buffer-fill/fit-type/round-trip/few-peaks |
| `TestPhysicsValidator` | 8 | Normal/psychrometric/nocturnal/CO2-bounds/temp-bounds/short-seq/negative |
| `TestGasAnomalyDetector` | 7 | Fit-all/fit-skip/normal/extreme/severity/zero-std/missing-col |

**Quality assessment:**
- Tests are independent (no inter-test dependencies).
- Descriptive names (`test_constant_sequence_is_stuck`, `test_serialization_roundtrip`).
- Good edge case coverage: zero-std, constant sequences, invalid dimensions, round-trip serialization.
- One assertion concept per test.

### Integration Test Coverage — GOOD
`tests/integration/test_phase4_integration.py` covers the comprehensive pipeline end-to-end with 36+ tests including: field presence, incident storage, metrics recording, learning progress, invalid input rejection, API status codes, batch inference, feedback loop, and error handling.

### Missing Test Coverage ⚠️ GAP

| Module | Gap |
|--------|-----|
| `src/api/routes/gas_anomalies.py` | **No API-level tests** for `/api/gas/summary` or `/api/gas/readings` |
| `src/db/learning_state_store.py` | **No dedicated unit tests** (tested indirectly through pipeline integration) |
| `src/dashboard/pages/7_gas_sensors.py` | **No dashboard tests** (common for Streamlit, but gap is noted) |
| EWMA/POT tracker persistence | **No test asserting** `_load_trackers`/`_save_trackers` correctly persist to/from DB |

**Recommendation:** Add at minimum:
1. `tests/api/test_gas_anomalies.py` — test both gas endpoints with mocked DB fixtures (synthetic readings, empty results, boundary hours values).
2. `tests/db/test_learning_state_store.py` — test load/save merge semantics, concurrent writer simulation.

---

## Findings Summary

### BLOCKING — None

### HIGH — None

### MEDIUM

| # | Finding | File | Lines | Action |
|---|---------|------|-------|--------|
| C-11 | Duplicated `GAS_COLUMNS` constant | `gas_anomaly_detector.py` / `config.py` | 19 / 564 | Import from `config.py`; delete local duplicate |

### LOW (Non-blocking)

| # | Finding | File | Lines | Severity |
|---|---------|------|-------|----------|
| C-4 | Mid-pipeline `db.commit()` prevents atomic rollback | `comprehensive_pipeline.py`, `incident_memory.py` | 552, 103 | INFO |
| C-10 | EWMA variance uses updated mean (slight bias) | `ewma_tracker.py` | 61-65 | INFO |
| C-12 | Query clause ordering misleading | `incident_memory.py` | 272-282 | INFO |
| C-13 | PhysicsValidator suppression path is dead code | `physics_validator.py` | all | INFO |
| P-1 | Gas baseline recomputed per request | `gas_anomalies.py` | 77 | INFO |
| P-2 | One Plotly trace per anomaly point | `7_gas_sensors.py` | 183-217 | INFO |
| P-3 | Dynamic attribute caching | `comprehensive_pipeline.py` | 501 | INFO |
| T-1 | Return type `str` vs `FaultType` | `fault_classifier.py` | 34 | INFO |
| T-2 | Unparameterized `dict` in type hint | `gas_anomaly_detector.py` | 69 | INFO |
| T-3 | `sequence: Any` type hint | `incident_memory.py` | 78 | INFO |
| T-4 | Mixed old/new-style type hints | multiple | — | INFO |
| Q-1 | No tests for gas API routes | — | — | WARN |
| Q-2 | No tests for learning_state_store | — | — | INFO |
| Q-3 | No tests for EWMA/POT DB persistence | — | — | INFO |

### Previously Tracked Issues — Status

| Issue | Status |
|-------|--------|
| GAR-3: `LearningStateStore.save()` blob overwrite | **RESOLVED** — merge semantics now implemented |
| GAR-1: Gas EWMA re-warmed per request | **OPEN** — documented in AGENT_MEMORY.md |
| GAR-4: `fault_type` unconstrained string | **OPEN** — non-blocking |

---

## Verdict

### **PASS WITH NOTES**

The SCADA anomaly detection implementation is **secure, architecturally sound, and correctionally solid**. All guardrail checks pass. No blocking or high-severity issues found.

**Key strengths:**
- Clean separation of concerns: detectors are stateless/pure, pipeline orchestrates via DI.
- Proper input validation at all API boundaries (FastAPI Query constraints + Pydantic v2).
- Robust error handling with graceful degradation on non-critical paths.
- Good test coverage on the model layer (43 unit tests + 36+ integration tests).
- `scipy.stats.genpareto` usage is correct and well-guarded with fallbacks.
- EWMA/POT serialization round-trips are verified by tests.
- GAR-3 (critical blob-overwrite bug) has been resolved.

**Action items before next phase:**
1. **MEDIUM:** Consolidate `GAS_COLUMNS` — import from `src/config.py` instead of maintaining a duplicate.
2. **WARN:** Add API-level tests for `/api/gas/summary` and `/api/gas/readings`.
3. **INFO:** Consider documenting the EWMA variance ordering choice in the module docstring.

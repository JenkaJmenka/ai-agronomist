# Full SCADA Anomaly Detection — Architecture Review & Improvement Plan

**Date:** 2026-06-21
**Reviewer:** Architect Agent
**Scope:** Full-stack review of Phases 1–5 + GAR-3 hotfix implementation
**Status:** Complete — prioritized improvement plan below

---

## Executive Summary

The SCADA anomaly detection system delivers a functional multi-layer detection pipeline (LSTM-VAE ensemble → fault classification → physics validation → adaptive thresholding → incident storage → active learning). The architecture is generally sound: modules are well-separated, config is centralized, and the dependency graph flows correctly (entry → logic → data). However, five structural risks threaten production reliability and should be addressed before scaling to multi-tenant production.

**Verdict:** The system works correctly for single-tenant demo workloads. Moving to multi-tenant production requires fixing the state contention issues (P1), unifying the detection contracts (P2), and constraining the unchecked string types (P2).

---

## 1. What Is Sound

### 1.1 Module Boundaries
- Clean separation: `src/models/` contains pure detection logic (no DB, no FastAPI imports), `src/api/` handles I/O, `src/db/` handles persistence. Dependencies flow one direction.
- `FaultClassifier`, `StuckFaultDetector`, `BiasOffsetDetector`, `PhysicsValidator` are all stateless — correct for pre-check layers.
- `EWMATracker` and `POTThresholdFitter` have clean `to_dict()`/`from_dict()` serialization contracts, making them persistence-agnostic.
- `EnsembleAnomalyDetector` correctly delegates to 4 independent sub-detectors via a `DetectorProtocol`, enabling future detector additions without modifying the ensemble.

### 1.2 Config Centralization
- All thresholds, model hyperparameters, and detection parameters live in `src/config.py`. No magic numbers in detection logic.
- Sensor names, ranges, and units are single-source-of-truth constants.
- Exception: `_BIAS_FRACTION = 0.3` in `bias_detector.py` (module-level, not in config — see P3-5).

### 1.3 Fault Taxonomy Design
- `FaultType` enum in `fault_classifier.py` correctly defines 5 canonical types.
- `FaultClassifier.classify()` is stateless and deterministic — first-match priority rules are clear.
- Ensemble pre-checks (stuck → bias → pattern classifier) have well-defined short-circuit semantics.

### 1.4 Physics Validation Layer
- `PhysicsValidator` is stateless, operates on denormalized physical units (correct), and returns a frozen `ValidationResult` dataclass.
- Pipeline correctly routes: confirmed faults bypass ensemble; suppressed faults exit early without storing incidents.

### 1.5 Adaptive Thresholding
- EWMA + POT dual-threshold design is statistically sound: EWMA tracks distribution shift, POT provides EVT-grounded tail thresholds.
- `_save_trackers` uses merge semantics (post GAR-3 hotfix) — no longer clobbers other keys in `state_json`.
- Warm-up guards (`_MIN_OBS=10` for EWMA, `_MIN_BUFFER_FILL=20` for POT) return `float("inf")` to disable dynamic thresholds until sufficient data exists.

---

## 2. Findings & Improvement Plan

### P1 — Critical (fix before multi-tenant production)

#### P1-1: LearningState.state_json Contention (GAR-3 residual)

**Status:** GAR-3 hotfix changed `LearningStateStore.save()` to merge semantics. But the underlying issue remains: a single JSON blob stores unrelated concerns (orchestrator state, EWMA+POT trackers, potentially future gas trackers) with no concurrency control.

**Risk:** Two concurrent requests for the same tenant can read-modify-write the same JSON blob. Both read the current blob, both merge their key, one overwrites the other's merge. Under load, EWMA/POT state and orchestrator state will silently diverge. SQLite serializes writes (mitigating this), but PostgreSQL does not.

**Where:** `src/db/learning_state_store.py:32-57` (`save()`) and `src/api/services/comprehensive_pipeline.py:525-555` (`_save_trackers()`).

**Fix:**
1. **Short-term:** Add `SELECT ... FOR UPDATE` (Postgres) or wrap the read-modify-write in an explicit transaction with retry-on-conflict. This prevents concurrent merge races.
2. **Long-term:** Split `LearningState.state_json` into dedicated tables:
   - `ewma_pot_state` (customer_id PK, ewma_json, pot_json, updated_at)
   - `orchestrator_state` (customer_id PK, state_json, updated_at)
   - `gas_detector_state` (future — customer_id, column_name, tracker_json)

   This eliminates the shared-blob contention entirely and makes each writer responsible for only its own table.

**Impact:** Silent data loss under concurrent writes → incorrect adaptive thresholds → missed or false anomalies.

---

#### P1-2: Pipeline Caches Trackers on Instance Attributes via setattr

**Where:** `src/api/services/comprehensive_pipeline.py:501-502,515,534`

**Problem:** `_load_trackers` caches EWMA/POT state on the pipeline instance using `setattr(self, f"_trackers_{customer_id}", ...)`. This creates unbounded instance attribute growth — one attribute per distinct `customer_id` that calls `run()`. In a long-lived process serving hundreds of tenants, this is a memory leak.

**Fix:** Replace with a bounded `dict` cache (like the existing `MAX_PIPELINE_CACHE_SIZE` pattern used for `_pipeline_cache` in `inference_integrated.py`). Use `OrderedDict` with LRU eviction, keyed by `customer_id`, capped at a configurable size.

```
# In config.py
MAX_TRACKER_CACHE_SIZE: int = 50

# In ComprehensiveInferencePipeline.__init__
self._tracker_cache: OrderedDict[str, Tuple[EWMATracker, POTThresholdFitter]] = OrderedDict()
```

**Impact:** Memory leak proportional to number of distinct tenants served.

---

#### P1-3: Every Pipeline Invocation Stores an Incident (even non-anomalies)

**Where:** `src/api/services/comprehensive_pipeline.py:378-390`

**Problem:** `memory.store_incident(...)` is called unconditionally in `_run_with_components`, regardless of whether `is_anomaly` is True or False. Every inference request — normal or anomalous — creates an Incident row with an embedding, a full sequence, root cause analysis, etc. This means:
- The `incidents` table grows at inference request rate, not anomaly rate
- Non-anomaly "incidents" pollute similarity search, recurring-issue analysis, and statistics
- Storage costs are 100-1000× what they should be (normal data far outnumbers anomalies)

**Fix:** Gate incident storage on `is_anomaly`:
```python
incident_id = None
if is_anomaly:
    incident_id = await memory.store_incident(...)
```

The suppressed-result path already returns `incident_id: None` — this makes the main path consistent.

**Impact:** Database bloat, polluted analytics, degraded similarity search quality.

---

### P2 — High (fix before adding more subsystems)

#### P2-1: Gas Pipeline vs Main Pipeline Contract Divergence (GAR-5)

**Status:** Carried from previous review. No shared `AnomalyResult` type.

**Problem:** Two detection subsystems with different contracts:

| Aspect | Main Pipeline | Gas Pipeline |
|--------|--------------|--------------|
| Result type | 30+ key `Dict[str, Any]` | `Dict[str, GasAnomaly]` |
| State | Stateful (DB-persisted EWMA+POT) | Stateless (per-request) |
| Scoring | LSTM-VAE + ensemble + physics | z-score + IQR |
| Fault classification | FaultClassifier → 5 types | None (severity only) |
| Incident storage | Yes (every run, see P1-3) | No |
| Confidence | ConfidenceScorer → [0,1] | z-score raw |

Adding a third subsystem (e.g., vibration, electrical) would create a third contract. This fan-out makes it impossible to build unified dashboards, alerting, or correlation analysis.

**Fix:** Define a shared `DetectionResult` dataclass:
```python
@dataclass
class DetectionResult:
    subsystem: str           # "main", "gas", future: "vibration"
    is_anomaly: bool
    score: float             # normalized [0, 1]
    confidence: float        # [0, 1]
    severity: str            # "normal", "warning", "critical"
    method: str              # detection method that triggered
    fault_type: Optional[str]
    affected_sensors: List[str]
    timestamp: datetime
    tenant_id: str
    greenhouse_id: str
    metadata: Dict[str, Any] # subsystem-specific details
```

Both pipelines produce `DetectionResult`; downstream consumers (dashboards, alerting, incident storage) consume one type.

**Impact:** Architectural divergence blocks unified monitoring, alerting, and cross-subsystem correlation.

---

#### P2-2: fault_type is Unconstrained String (GAR-4)

**Status:** Carried from previous review. `FaultType` enum exists but is not enforced.

**Where:** `Incident.fault_type` is `String(32)` in `src/db/models.py:175-177`. `IncidentCreateRequest.fault_type` is `Optional[str]` with `max_length=32` in `src/api/schemas/incidents.py:33-34`. No validation against `FaultType` enum values.

**Problem:** Any string ≤32 chars can be stored as `fault_type`. The enum exists in `fault_classifier.py` but is not imported or enforced at the schema or DB level. Future code changes or manual DB edits can introduce invalid values that break dashboard filters and analytics.

**Fix:**
1. Add a Pydantic `field_validator` to `IncidentCreateRequest`:
   ```python
   from src.models.fault_classifier import FaultType
   VALID_FAULT_TYPES = {ft.value for ft in FaultType}

   @field_validator("fault_type")
   @classmethod
   def validate_fault_type(cls, v):
       if v is not None and v not in VALID_FAULT_TYPES:
           raise ValueError(f"Invalid fault_type: {v}")
       return v
   ```
2. Add a DB-level CHECK constraint in the next Alembic migration:
   ```sql
   ALTER TABLE incidents ADD CONSTRAINT ck_fault_type
   CHECK (fault_type IS NULL OR fault_type IN
     ('calibration_drift','bias_offset','stuck_fault','accuracy_decline','spike_fault'));
   ```
3. Add the same validator to the pipeline's `store_incident` call site.

**Impact:** Data integrity — invalid fault types silently persist, breaking downstream filters.

---

#### P2-3: Gas EWMA is Per-Request Only (GAR-1)

**Status:** Carried from previous review. EWMA warm-up from historical window produces identical results to a static threshold.

**Where:** `src/models/gas_anomaly_detector.py:116-120` (warm-up in `fit()`) and `src/api/routes/gas_anomalies.py:76` (fresh detector per request).

**Fix (recommended):** Remove the EWMA layer from `GasAnomalyDetector` entirely. The z-score + IQR combination is sufficient for within-window batch anomaly scoring. EWMA adds ~N forward passes of overhead per `fit()` call with zero cross-request benefit.

If cross-request drift detection is desired for gas sensors in the future, implement it as a separate `GasEWMAService` with DB persistence — do not embed it in the stateless detector.

**Impact:** Misleading statefulness, wasted compute, no actual adaptive behavior.

---

#### P2-4: Comprehensive Pipeline Returns Untyped Dict

**Where:** `src/api/services/comprehensive_pipeline.py:414-455` — `_run_with_components` returns a 30+ key `Dict[str, Any]`.

**Problem:** No schema validation on the pipeline output. Consumers (routes, tests, dashboards) must guess which keys exist and what types they are. Adding or renaming a key silently breaks downstream code with no compile-time or startup-time detection.

**Fix:** Define a `PipelineResult` Pydantic model (or dataclass) as the return type:
```python
class PipelineResult(BaseModel):
    is_anomaly: bool
    anomaly_score: float
    confidence: float
    confidence_level: str
    pattern_type: str
    fault_type: Optional[str]
    # ... all 30+ fields typed
```

This enforces the contract at the boundary and gives IDE/type-checker support to all consumers.

**Impact:** Fragile contract — silent breakage on field changes, no type safety for consumers.

---

### P3 — Medium (improve during next development cycle)

#### P3-1: PhysicsValidator Has No Suppression Rules

**Where:** `src/models/physics_validator.py:57-132`

**Problem:** All 4 rules return `confirmed=True`. No rule sets `suppressed=True`. The pipeline's suppression branch (`if physics_result.suppressed:` at line 263) is dead code. The original spec mentions suppression (e.g., "ventilation-caused temperature spikes during scheduled maintenance"), but no suppression rules were implemented.

**Fix:** Add at least one suppression rule (e.g., gradual temperature changes within normal diurnal range when humidity moves inversely — psychrometric normal behavior). Or, if suppression is intentionally deferred, document this explicitly and remove the dead branch from the pipeline (it creates a false impression of a complete suppression system).

**Impact:** Dead code path; false positives from physics-explainable patterns are not filtered.

---

#### P3-2: Ensemble Baseline Comparison Uses np.array_equal (O(n) every call)

**Where:** `src/api/services/comprehensive_pipeline.py:306-309` and `src/models/ensemble_detector.py:306-309`

**Problem:**
```python
if self._baseline_cache is None or not np.array_equal(
    self._baseline_cache, baseline
):
    self.fit_baseline(baseline)
```
Every `detect()` call performs an element-wise array comparison of the full baseline tensor (potentially `(N, 20, 4)` floats). For N=1000 baseline sequences, this is 80K float comparisons per inference call.

**Fix:** Use a hash-based cache key (e.g., hash of baseline's `.tobytes()`) or an identity check (store the baseline's `id()` or a version counter). This reduces the check from O(N×seq_len×input_dim) to O(1).

**Impact:** Unnecessary compute per inference call; latency impact grows with baseline size.

---

#### P3-3: BiasOffsetDetector Threshold Not in Config

**Where:** `src/models/bias_detector.py:18` — `_BIAS_FRACTION: float = 0.3`

**Problem:** This is a detection threshold controlling when a sensor is flagged as biased. It's defined as a module-level constant instead of in `src/config.py`. All other detection thresholds are centralized in config.

**Fix:** Move to `src/config.py`:
```python
BIAS_OFFSET_FRACTION: float = 0.3
```

Also move the module-level constants from `physics_validator.py` (`_TEMP_TREND_THRESHOLD`, `_HUM_TREND_THRESHOLD`, `_NOCTURNAL_LIGHT_THRESHOLD`, `_NIGHT_HOURS`, `_CO2_LOW`, `_CO2_HIGH`, `_TEMP_LOW`, `_TEMP_HIGH`) to config for consistency and per-deployment tunability.

**Impact:** Inconsistent config pattern; these thresholds are not tunable without code changes.

---

#### P3-4: FaultClassifier Default Fallback Returns spike_fault

**Where:** `src/models/fault_classifier.py:66` — `return FaultType.SPIKE_FAULT.value`

**Problem:** When no pattern match and no high noise level, the classifier defaults to `spike_fault`. This silent default can mask unrecognized anomaly types. A more explicit approach would be to return `None` or a dedicated `"unknown"` type and let the caller decide.

**Fix:** Add a `FaultType.UNKNOWN = "unknown"` enum value and use it as the default. This makes it possible for dashboards and analytics to track how often the classifier falls through to the default.

**Impact:** Silent misclassification of unrecognized patterns as spike faults.

---

#### P3-5: Gas Anomaly Route Has No Response Model

**Where:** `src/api/routes/gas_anomalies.py:58,140` — `@router.get("/summary")` and `@router.get("/readings")` have no `response_model`.

**Problem:** Without a Pydantic response model, FastAPI does not validate the response shape, and the OpenAPI spec shows `Any` as the response type. Clients get no schema documentation.

**Fix:** Define `GasSummaryResponse` and `GasReadingsResponse` Pydantic models in `src/api/schemas/gas.py` and wire them via `response_model=`.

**Impact:** No API contract enforcement for gas endpoints; poor developer experience for consumers.

---

#### P3-6: PhysicsValidator Uses UTC Hour for Night Detection

**Where:** `src/models/physics_validator.py:46,100`

**Problem:** `_NIGHT_HOURS` uses UTC hours `{22, 23, 0, 1, 2, 3, 4}`. For a greenhouse in UTC+3, "midnight UTC" is 3 AM local — the night hours are wrong. For UTC-8, "midnight UTC" is 4 PM local — every afternoon reading with lights on gets flagged as "nocturnal light."

**Fix:** Either:
- Accept a timezone offset or local hour as an argument to `validate()`
- Store greenhouse timezone in `customer_model_config` and load it during validation
- Remove the nocturnal light rule until timezone support is implemented

**Impact:** False positive/negative classifications for any greenhouse not in UTC±1.

---

### P4 — Low (polish, tech debt, best practices)

#### P4-1: GAS_COLUMNS Defined in Two Places

**Where:** `src/models/gas_anomaly_detector.py:19` and `src/config.py:564-571` (`GAS_SENSOR_COLUMNS`)

**Problem:** `gas_anomaly_detector.py` defines its own `GAS_COLUMNS` list and also has a class-level `GAS_COLUMNS`. `src/config.py` defines `GAS_SENSOR_COLUMNS` with the same values. Routes import from the detector module. Two sources of truth for gas column names.

**Fix:** Remove `GAS_COLUMNS` from `gas_anomaly_detector.py`. Import `GAS_SENSOR_COLUMNS` from config. Update routes to import from config.

---

#### P4-2: POT Module-Level Constants Not in Config

**Where:** `src/models/pot_threshold.py:21-24` — `_MIN_BUFFER_FILL=20`, `_REFIT_INTERVAL=50`

**Problem:** These control how often the GPD is re-fitted and the minimum buffer size. They're not in `src/config.py`, making them non-tunable without code changes.

**Fix:** Move to config as `POT_MIN_BUFFER_FILL` and `POT_REFIT_INTERVAL`.

---

#### P4-3: StuckFaultDetector and BiasOffsetDetector Hardcode 4-Sensor Assumption

**Where:** `src/models/stuck_fault_detector.py:50-56`, `src/models/bias_detector.py:55-79`

**Problem:** Both detectors use `min(sequence.shape[1], len(_SENSOR_NAMES))` to handle varying column counts. While safe, they silently ignore extra columns. If `INPUT_DIM` or `SENSOR_NAMES` is extended (e.g., adding soil moisture), these detectors work but only analyze the first N channels.

**Fix:** Accept `sensor_names` as a constructor parameter (defaulting to `config.SENSOR_NAMES`) instead of importing a module-level copy. This aligns with `PatternDetector`'s pattern.

---

#### P4-4: Ensemble IsolationForest Uses `assert self._model is not None`

**Where:** `src/models/ensemble_detector.py:142`

**Problem:** Uses `assert` for a runtime condition (model might not be fitted). In optimized mode (`python -O`), assertions are stripped, and the unfitted model proceeds silently.

**Fix:** Replace with an explicit check:
```python
if self._model is None:
    raise RuntimeError("IsolationForest not fitted — call fit() or pass baseline")
```

Same issue at lines 179 and 205.

---

#### P4-5: _PHYSICS_REASON_TO_FAULT Mapping is Incomplete

**Where:** `src/api/services/comprehensive_pipeline.py:56-61`

**Problem:** All physics reasons currently map to either `spike_fault` or `calibration_drift`. As more physics rules are added, this mapping must be updated manually. There's no validation that all possible `PhysicsValidator` reasons are covered.

**Fix:** Add a startup-time validation that all defined physics reasons have a mapping, or derive the mapping from the physics rule itself (each rule could carry its fault_type).

---

#### P4-6: POTThresholdFitter.fit() Lazy-Imports scipy on Every Call

**Where:** `src/models/pot_threshold.py:86` — `from scipy.stats import genpareto`

**Problem:** The import runs inside `fit()`, which is called every `_REFIT_INTERVAL` observations. While Python caches imports, the import machinery still runs the lookup. For a module called frequently in a streaming context, this is unnecessary overhead.

**Fix:** Move to a module-level import with a `try/except ImportError` fallback, or import once at class level.

---

#### P4-7: ComprehensiveInferencePipeline Creates New Attributor/PatternDetector Per Call

**Where:** `src/api/services/comprehensive_pipeline.py:176-180` (in `run()`)

**Problem:** Each `run()` call creates a fresh `AnomalyAttributor` (which calls `model.eval()`) and `PatternDetector`, then calls `set_baseline()`. For the `run_batch()` path, these are correctly reused across sequences. But for sequential `run()` calls with the same baseline, the setup cost is repeated.

**Fix:** Cache the attributor and pattern detector on the pipeline instance, keyed by baseline identity (similar to the ensemble's `_baseline_cache` pattern). Rebuild only when the baseline changes.

---

## 3. Data Flow Assessment

### Current Flow (correct path)
```
Request → seq validation → LSTM-VAE forward pass → physics validation
    ├─ suppressed → return minimal result (no incident)
    ├─ confirmed → bypass ensemble, set fault_type from physics
    └─ unclassified → ensemble detection (4 detectors)
        → fault classification (stuck → bias → pattern → default)
        → EWMA/POT threshold update
        → confidence scoring
        → root cause analysis → recommendations
        → incident storage → online learner observe
        → metrics recording → response
```

### Issues in Flow
1. **Incident storage is not gated on `is_anomaly`** (P1-3)
2. **EWMA/POT load uses broad `except Exception` catch** — masks DB connectivity issues
3. **Online learner observe is gated on `is_anomaly`** — correct
4. **Suppressed path is dead** — no rules produce `suppressed=True` (P3-1)

---

## 4. Persistence Strategy Assessment

| Data | Store | Pattern | Risk |
|------|-------|---------|------|
| Sensor readings | `sensor_readings` table | Append-only, tenant-indexed | Sound |
| Raw readings | `raw_readings` table | Append-only, JSON data column | Sound |
| Incidents | `incidents` table | Append-only + resolve mutation | P1-3: stores non-anomalies |
| EWMA/POT state | `learning_states.state_json` | Shared JSON blob, merge-write | P1-1: concurrent contention |
| Orchestrator state | `learning_states.state_json` | Shared JSON blob, merge-write | P1-1: same blob |
| Gas EWMA state | Not persisted | Per-request warm-up | P2-3: no cross-request memory |
| Metrics snapshots | `metrics_snapshots` table | Periodic flush | Sound |
| Learning logs | `learning_logs` table | Append-only audit trail | Sound |

**Key recommendation:** Split `learning_states` into purpose-specific tables (P1-1 long-term fix).

---

## 5. Separation of Concerns Assessment

| Layer | Current State | Assessment |
|-------|--------------|------------|
| Detection (`src/models/`) | Pure logic, no DB/API imports | ✅ Clean |
| API routes (`src/api/routes/`) | I/O + light orchestration | ✅ Clean |
| Pipeline service (`src/api/services/`) | Orchestration + DB access | ⚠️ Does too much — 572 lines |
| DB models (`src/db/models.py`) | ORM definitions | ✅ Clean |
| Config (`src/config.py`) | Centralized constants | ⚠️ 579 lines, growing — consider splitting by domain |

The `ComprehensiveInferencePipeline` class is the main concern: it orchestrates detection, attribution, pattern analysis, physics validation, fault classification, adaptive thresholding, confidence scoring, root cause analysis, recommendations, incident storage, online learning, and metrics — all in one 572-line class. While each step correctly delegates to a dedicated module, the orchestration itself is becoming a god-class.

**Recommendation:** Extract two sub-pipelines:
1. `DetectionPipeline` — LSTM-VAE + ensemble + physics + fault classification → `DetectionResult`
2. `AnalysisPipeline` — attribution + pattern + confidence + root cause + recommendations → `AnalysisResult`

The `ComprehensiveInferencePipeline` becomes a thin orchestrator that calls DetectionPipeline → AnalysisPipeline → incident storage.

---

## 6. Implementation Priority & Effort Matrix

| ID | Priority | Effort | Description |
|----|----------|--------|-------------|
| P1-1 | Critical | Medium | LearningState contention — add row-level locking; plan table split |
| P1-2 | Critical | Low | Bound tracker cache — replace setattr with OrderedDict |
| P1-3 | Critical | Low | Gate incident storage on is_anomaly |
| P2-1 | High | High | Shared DetectionResult contract |
| P2-2 | High | Low | Constrain fault_type — Pydantic validator + DB CHECK |
| P2-3 | High | Low | Remove EWMA from GasAnomalyDetector |
| P2-4 | High | Medium | Type pipeline output — PipelineResult model |
| P3-1 | Medium | Medium | Add physics suppression rules or remove dead branch |
| P3-2 | Medium | Low | Hash-based baseline cache in ensemble |
| P3-3 | Medium | Low | Move threshold constants to config |
| P3-4 | Medium | Low | Add FaultType.UNKNOWN default |
| P3-5 | Medium | Low | Add Pydantic response models for gas endpoints |
| P3-6 | Medium | Medium | Timezone-aware nocturnal light detection |
| P4-1 | Low | Low | Deduplicate GAS_COLUMNS |
| P4-2 | Low | Low | Move POT constants to config |
| P4-3 | Low | Low | Accept sensor_names in detector constructors |
| P4-4 | Low | Low | Replace assert with RuntimeError |
| P4-5 | Low | Low | Validate physics-reason-to-fault mapping |
| P4-6 | Low | Low | Module-level scipy import |
| P4-7 | Low | Medium | Cache attributor/pattern detector on pipeline |

---

## 7. Recommended Implementation Order

### Sprint A (P1 — Critical, 1-2 days)
1. **P1-3** — Gate incident storage on `is_anomaly` (5 min, 1-line change)
2. **P1-2** — Replace `setattr` tracker cache with bounded dict (30 min)
3. **P1-1 short-term** — Add `SELECT ... FOR UPDATE` to `LearningStateStore.save()` (1 hr)

### Sprint B (P2 — High, 2-3 days)
1. **P2-2** — Constrain `fault_type` with validator + migration (1 hr)
2. **P2-3** — Remove EWMA from `GasAnomalyDetector` (30 min)
3. **P2-4** — Define `PipelineResult` model (2 hrs)
4. **P2-1** — Define shared `DetectionResult` contract, refactor gas + main pipelines (4 hrs)

### Sprint C (P3 — Medium, 2 days)
1. **P3-3, P3-5** — Config + response model cleanup (1 hr each)
2. **P3-2** — Hash-based baseline cache (30 min)
3. **P3-4** — FaultType.UNKNOWN (30 min)
4. **P3-1** — Physics suppression rules or dead code removal (2 hrs)
5. **P3-6** — Timezone support for nocturnal light (2 hrs)

### Sprint D (P4 — Low, ongoing)
- Address during related feature work; no dedicated sprint needed.

### Long-term (next quarter)
- **P1-1 long-term** — Split `learning_states` into dedicated tables + migration
- **Pipeline decomposition** — Extract DetectionPipeline + AnalysisPipeline from ComprehensiveInferencePipeline

---

## 8. Previously Flagged Items — Status

| ID | Finding | Status |
|----|---------|--------|
| GAR-1 | Gas EWMA per-request only | Open → P2-3 above |
| GAR-3 | LearningState blob clobber | Hotfixed (merge semantics) → residual contention P1-1 |
| GAR-4 | fault_type unconstrained | Open → P2-2 above |
| GAR-5 | Gas vs main contract divergence | Open → P2-1 above |

---

*Generated by Architect Agent — 2026-06-21*

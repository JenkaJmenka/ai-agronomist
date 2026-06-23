# Gap Fix Architecture Review

**Date:** 2026-06-20
**Reviewer:** Architect Agent
**Scope:** Gaps 1-3 structural assessment (EWMA layer, LearningState persistence, fault_type propagation)
**Status:** Review complete -- action items recommended

---

## 1. Stateful Detector in a Stateless Route

### Finding: EWMA state is lost between requests (HIGH risk)

`GasAnomalyDetector` (in `src/models/gas_anomaly_detector.py`) holds per-column
`EWMATracker` instances in `self._ewma_trackers`, populated during `fit()`. In
`src/api/routes/gas_anomalies.py` line 76, a fresh detector is instantiated on
every request:

```
detector = GasAnomalyDetector()
baselines = detector.fit(readings_data)
```

The EWMA trackers are warmed from the historical window on each request, which
means they always start cold and re-derive state from the same batch of data
that was used to fit the baseline. This has two consequences:

1. **The EWMA threshold computed during `fit()` warm-up and then queried during
   `detect()` on the same request is mathematically identical to what a static
   threshold would produce.** The warm-up feeds the same z-scores that defined
   the baseline; by the end of warm-up the EWMA mean converges to the baseline
   mean-z and the variance converges to the baseline z-variance. Querying
   `is_anomaly(z)` on the same data that warmed the tracker will only flag
   readings that are extreme relative to the same window. There is no temporal
   adaptation happening -- the EWMA adds compute cost without providing the
   intended benefit of tracking distribution shift over time.

2. **Cross-request continuity is absent.** If a gas sensor drifts slowly across
   multiple API calls, the EWMA tracker cannot detect the drift because it has
   no memory of prior calls. Each call fits its own 1-168 hour window
   independently.

### Recommendation

Either:

**(A) Remove the EWMA layer from `GasAnomalyDetector`** if the intended design
is purely request-scoped statistical detection (z-score + IQR is sufficient for
batch anomaly scoring within a single window). This simplifies the detector,
removes the misleading statefulness, and aligns with how the route actually uses
it.

**(B) Persist the gas detector's EWMA state per-tenant** in the same pattern as
the main pipeline. This means:

- Add `_load_gas_trackers` / `_save_gas_trackers` to the gas route (or a thin
  service layer).
- Store the state in a dedicated key in `LearningState.state_json` (e.g.,
  `gas_ewma_trackers`) or -- preferably -- in a dedicated table (see Finding 3
  below).
- On each request, load the persisted trackers, update them with new z-scores,
  and save them back. Do not re-warm from scratch.

Option (A) is recommended for the current product stage. The gas pipeline is a
secondary detection subsystem with no model component; the z-score + IQR
combination already handles within-window anomalies well. EWMA is valuable for
cross-window drift detection, but that requires persistence infrastructure that
does not yet exist for gas sensors.

---

## 2. Dual EWMA Persistence Strategies

### Finding: Inconsistency is acceptable short-term, problematic long-term (MEDIUM risk)

The system now has two EWMA strategies:

| Subsystem | Persistence | State lifetime | Drift detection |
|-----------|------------|----------------|-----------------|
| Main pipeline (LSTM-VAE) | DB (`LearningState.state_json["ewma_pot_trackers"]`) | Across requests, across restarts | Yes -- EWMA tracks distribution shift over time |
| Gas pipeline | None (re-created from window data each request) | Single request | No -- each request is independent |

**Trade-offs of the current dual approach:**

- **Simplicity:** The gas pipeline avoids persistence complexity. For a
  secondary detection path with low request volume, this is pragmatic.
- **Inconsistency:** A user monitoring both subsystems will see different
  sensitivity behaviors. The main pipeline adapts its thresholds over time; the
  gas pipeline does not. A slow gas sensor drift that unfolds across multiple
  API calls will be invisible to the gas detector but would (if persisted) be
  caught by the main pipeline's EWMA.
- **Operational confusion:** If gas EWMA is retained without persistence (as
  currently implemented), the EWMA flags on gas anomalies are unreliable -- they
  are warm-up artifacts, not genuine adaptive thresholds.

### Recommendation

This is acceptable for the current product stage, provided:

1. The EWMA layer is removed from `GasAnomalyDetector` (per Finding 1,
   option A). Without persistence, EWMA adds false complexity.
2. When gas sensor monitoring matures to require cross-request adaptive
   thresholds, adopt the same persistence pattern as the main pipeline. At
   that point, unify both into a single `TrackerPersistence` service (see
   Finding 3).

---

## 3. Shared JSON Blob `state_json` -- Write Contention

### Finding: Two independent writers clobber each other's data (CRITICAL risk)

`LearningState.state_json` is shared between two writers:

**Writer 1 -- `LearningStateStore.save()`** (called from `save_orchestrator` in
`orchestrator_registry.py`): Replaces the ENTIRE `state_json` blob:

```python
row.state_json = state  # full replacement
```

The `state` dict comes from `RetrainingOrchestrator.export_state()`, which
serializes feedback history, operator reliability, retrain timestamps, etc.
This dict does NOT contain an `ewma_pot_trackers` key.

**Writer 2 -- `ComprehensiveInferencePipeline._save_trackers()`**: Merges a
key into the existing blob:

```python
current = dict(row.state_json or {})
current["ewma_pot_trackers"] = payload
row.state_json = current
```

**The race condition:**

1. Pipeline runs inference -> `_save_trackers()` merges `ewma_pot_trackers`
   into `state_json`. Blob now contains both orchestrator state and tracker
   state.
2. Operator provides feedback -> `save_orchestrator()` calls
   `LearningStateStore.save()` which does `row.state_json = state` where
   `state` is the pure orchestrator export. This **overwrites the entire blob**,
   erasing `ewma_pot_trackers`.
3. Next inference call -> `_load_trackers()` finds no `ewma_pot_trackers` key
   in the blob -> falls back to fresh trackers, losing all accumulated EWMA/POT
   state.

Even without concurrency, this is a data-loss bug: the two writers operate
sequentially within normal usage flow (inference writes trackers, then feedback
writes orchestrator state) and each one silently destroys the other's data.

### Severity

CRITICAL. The adaptive thresholds (EWMA + POT) that accumulate over many
inference calls are silently reset every time a single piece of operator feedback
is recorded. This defeats the purpose of persistent adaptive thresholds.

### Recommendation

**Immediate fix (minimal change):** Make `LearningStateStore.save()` perform a
merge instead of a full replacement:

```python
# In LearningStateStore.save():
current = dict(row.state_json or {})
current.update(state)  # merge, not replace
row.state_json = current
```

This is a one-line fix that prevents data loss, but it does not address the
underlying structural problem.

**Structural fix (recommended):** Separate the two concerns into distinct
storage:

- **Option 1: Namespaced keys.** Define a convention where each writer owns a
  top-level key in `state_json` (e.g., `orchestrator_state`, `ewma_pot_trackers`,
  `gas_ewma_trackers`). All writers use merge semantics. All readers access only
  their own key.

- **Option 2: Dedicated tables.** Create a `threshold_tracker_state` table with
  columns `(customer_id PK, tracker_type VARCHAR, state_json JSON, updated_at)`.
  This separates EWMA/POT state from orchestrator state at the schema level,
  eliminates the shared-blob contention, and naturally supports multiple tracker
  types (main pipeline EWMA+POT, gas EWMA) without key collisions.

Option 2 is architecturally cleaner and is recommended for the next migration.
Option 1 is acceptable as a stepping stone if migration effort must be deferred.

### Additional concern: no concurrency guard

Even with the merge fix, two concurrent requests writing to the same
`customer_id` row can still lose updates (last-write-wins on the JSON column).
For the current single-process async deployment this is unlikely (SQLAlchemy
async sessions serialize within a single event loop), but under multi-worker
deployment (e.g., Gunicorn with multiple Uvicorn workers), this becomes a real
risk. A `SELECT ... FOR UPDATE` or optimistic-concurrency check on `updated_at`
should be added when moving to multi-worker deployment.

---

## 4. fault_type Column and Incident Schema

### Finding: Unconstrained string for enum-like values (LOW risk, MEDIUM migration risk)

`Incident.fault_type` is `String(32), nullable=True`. The five fault types are
defined as a `FaultType(str, Enum)` in `src/models/fault_classifier.py`:

```
calibration_drift, bias_offset, stuck_fault, accuracy_decline, spike_fault
```

`FaultClassifier.classify()` returns `FaultType.XXX.value` -- a plain string.
The column stores this string without any database-level constraint.

**Current risks:**

1. **No DB-level enforcement.** Any code path that writes to `fault_type`
   directly (bypassing `FaultClassifier`) can store arbitrary strings. The
   column docstring documents the valid values, but documentation is not
   enforcement.

2. **Default fallback.** `FaultClassifier.classify()` returns `spike_fault` as
   the catch-all default (line 66). If the pattern vocabulary expands (e.g., a
   new `oscillation` pattern), the classifier silently bins it into
   `spike_fault` until a new rule is added. This is a silent misclassification,
   not a crash -- which may be harder to detect.

3. **Migration risk on rename.** If any fault type value changes (e.g.,
   `stuck_fault` -> `stuck_sensor`), all existing rows with the old value
   become orphaned. Queries filtering by fault type will silently exclude
   historical incidents. A data migration would be needed alongside the code
   change.

**What is acceptable:**

- Using a string column rather than a database-native ENUM is the correct
  choice for cross-backend compatibility (SQLite has no ENUM type).
- The `FaultType` Python enum ensures that `FaultClassifier` only produces
  valid values. The risk is limited to bypass paths.
- The 32-character limit is adequate for the current vocabulary.

### Recommendation

1. **Add a CHECK constraint in migration** (where supported). For PostgreSQL,
   this is straightforward:
   ```sql
   ALTER TABLE incidents ADD CONSTRAINT ck_fault_type
     CHECK (fault_type IS NULL OR fault_type IN (
       'calibration_drift','bias_offset','stuck_fault',
       'accuracy_decline','spike_fault'
     ));
   ```
   For SQLite, the CHECK can be added at table creation (existing data would
   need a migration test).

2. **Add a Pydantic validator** on `IncidentCreateRequest.fault_type` to reject
   values not in `FaultType`. This provides API-level enforcement without
   requiring a schema migration.

3. **Log unknown patterns** in `FaultClassifier.classify()` before returning the
   default. Currently the default return is silent; a `logger.warning()` would
   surface misclassification early.

4. **When renaming fault types**, always include a data migration step that
   updates existing rows.

---

## 5. Gas Sensor Anomaly Detection Architecture

### Finding: Architectural gap between subsystems is sustainable short-term, unsustainable at scale (MEDIUM risk)

| Dimension | Main pipeline | Gas pipeline |
|-----------|--------------|-------------|
| Model | LSTM-VAE deep learning | No model -- statistical (z-score + IQR) |
| State | Per-tenant persisted EWMA + POT + orchestrator | Request-scoped, no persistence |
| Thresholds | Adaptive (EWMA + POT, EVT-derived) | Fixed (z > 3.0 or IQR fence x 1.5) |
| Fault classification | FaultClassifier (5 categories) | None -- raw z-scores only |
| Incident storage | Full incident lifecycle (store, resolve, similar search) | None |
| Learning | Active learning + online fine-tuning | None |
| API interface | POST `/api/inference/comprehensive` (rich response) | GET `/api/gas/summary` (flat JSON) |

**Is this sustainable?**

For the current product stage (greenhouse environment monitoring where gas
sensors are secondary to the four core SCADA channels), the architectural gap is
acceptable. The gas pipeline serves a different use case: it provides real-time
statistical anomaly visibility over gas readings that arrive via CSV upload. It
does not need the full LSTM-VAE machinery.

**When it becomes unsustainable:**

- When gas anomalies need to trigger incidents (incident storage requires
  embeddings, which require a model).
- When operators need to provide feedback on gas anomalies (active learning
  requires incident IDs and the feedback loop).
- When gas sensor drift detection is needed across requests (EWMA persistence).
- When a unified anomaly dashboard must present both subsystems with consistent
  confidence scores, fault types, and recommendations.

### Recommendation: Unified detection interface (future work)

Do NOT unify the implementations today. The statistical gas detector is
appropriately simple for its current use case, and forcing it into the LSTM-VAE
pipeline would add unnecessary complexity.

Instead, define a **common anomaly result contract** that both subsystems
conform to:

```
AnomalyResult:
  is_anomaly: bool
  score: float          # 0-1 normalized
  confidence: float     # 0-1
  method: str           # "lstm_vae", "z_score", "iqr", "ewma"
  severity: str         # "normal", "warning", "critical"
  fault_type: str|None  # from FaultType enum
  sensor: str           # which sensor channel
  timestamp: datetime
```

Both the main pipeline's `ComprehensiveInferencePipeline.run()` and the gas
pipeline's `GasAnomalyDetector.detect()` would produce results conforming to
this contract. Downstream consumers (dashboards, incident storage, alerting)
would depend on the contract, not on the specific detector implementation.

This allows:

- Adding incident storage for gas anomalies without refactoring the gas
  detector.
- Building a unified anomaly timeline in the dashboard.
- Applying the same fault classification logic to gas anomalies.
- Eventual migration of gas detection to a model-based approach without
  changing downstream code.

---

## Summary of Action Items

| ID | Severity | Finding | Recommended action |
|----|----------|---------|-------------------|
| GAR-1 | HIGH | Gas EWMA state lost between requests | Remove EWMA from GasAnomalyDetector (or add persistence) |
| GAR-2 | MEDIUM | Dual EWMA strategies without persistence parity | Acceptable if GAR-1 is resolved by removing EWMA |
| GAR-3 | CRITICAL | LearningStateStore.save() clobbers ewma_pot_trackers | Immediate: merge semantics in save(). Structural: dedicated tracker table |
| GAR-4 | LOW | fault_type is unconstrained string | Add Pydantic validator; add CHECK constraint in migration |
| GAR-5 | MEDIUM | Gas and main pipeline architectural divergence | Define common AnomalyResult contract for future unification |

### Priority order

1. **GAR-3** -- Fix the data-loss bug in `LearningStateStore.save()`. This is a
   correctness issue that silently resets adaptive thresholds.
2. **GAR-1** -- Remove EWMA from `GasAnomalyDetector` to eliminate misleading
   statefulness.
3. **GAR-5** -- Design the common `AnomalyResult` contract before adding more
   detection subsystems.
4. **GAR-4** -- Add validation constraints on `fault_type`.
5. **GAR-2** -- Resolves automatically once GAR-1 is addressed.

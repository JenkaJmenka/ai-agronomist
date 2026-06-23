# Plan: SCADA Anomaly Detection — Research Gap Implementation

## Context
The research document "Greenhouse SCADA Sensor Anomaly Detection" defines a rigorous spec for fault detection in Priva-automated greenhouses. A gap analysis against the current codebase reveals six concrete gaps to close, across two focus areas: **ML/detection quality** (5 fault types, adaptive thresholds, physics validation) and **gas sensor anomaly detection** (C2H4, CO, NO, NO2, NOx). Priva/SCADA live integration is deferred.

---

## Gap Summary

| Gap | Current State | Target |
|-----|--------------|--------|
| Fault taxonomy | Pattern types (drift, spike, etc.) — not mapped to the 5 research types | Explicit `fault_type` on every Incident |
| Stuck fault | Not detected | Variance-based detector flags sensor frozen at constant value |
| Bias offset | Not detected | Rolling mean vs. expected mean divergence detector |
| EWMA dynamic threshold | Fixed `ANOMALY_THRESHOLD` in `config.py` | Per-tenant per-sensor EWMA adaptive threshold |
| POT adaptive threshold | Fixed threshold | Peaks Over Threshold (EVT/GPD) on residual tail distribution |
| Physics validation | CorrelationDetector detects some cases | Explicit psychrometric rule engine to suppress/confirm faults |
| Gas sensor detection | C2H4/CO/NO/NO2/NOx stored in RawReading JSON only | Statistical anomaly detector + dashboard page |

---

## Phase 1 — Fault Type Taxonomy + New Detectors

**New file: `src/models/fault_classifier.py`**
- `FaultClassifier.classify(pattern_type, sensor_stats) → FaultType`
- Enum `FaultType`: `calibration_drift | bias_offset | stuck_fault | accuracy_decline | spike_fault`
- Mapping rules:
  - `pattern=drift` → `calibration_drift`
  - `pattern=spike` → `spike_fault`
  - `pattern=saturation` → `stuck_fault` (falls back to stuck detector result)
  - High noise without directional trend → `accuracy_decline`
  - Persistent mean shift → `bias_offset`

**New file: `src/models/stuck_fault_detector.py`**
- `StuckFaultDetector(window=20, variance_threshold=1e-4)`
- `detect(sequence: np.ndarray) → dict[str, bool]` — per-sensor flag
- Rolling variance across the sequence window; flag if `var < variance_threshold` for all 20 timesteps

**New file: `src/models/bias_detector.py`**
- `BiasOffsetDetector(expected_ranges: dict)` — uses `SENSOR_RANGES` from `src/config.py`
- `detect(sequence: np.ndarray) → dict[str, float]` — per-sensor bias magnitude
- Computes sequence mean; compares to midpoint of expected range
- If `|mean - expected_mid| > 0.3 * range_span` → bias flag with magnitude

**Modify: `src/models/ensemble_detector.py`**
- Add `StuckFaultDetector` and `BiasOffsetDetector` as pre-checks run before the 4-detector vote
- Stuck fault detected → short-circuit, return `anomaly=True, fault_type=stuck_fault` immediately
- Bias detected → set `bias_flag=True`, pass to fault classifier alongside pattern type

**Modify: `src/api/services/comprehensive_pipeline.py`**
- Import and call `FaultClassifier.classify()` after pattern detection step
- Pass `fault_type` through to `IncidentMemory.store_incident()` (extend kwargs)

**Modify: `src/db/models.py`**
- Add `fault_type: Mapped[Optional[str]]` to `Incident` (String 32, nullable)

**New migration: `alembic/versions/010_add_fault_type_to_incidents.py`**
- `op.add_column("incidents", sa.Column("fault_type", sa.String(32), nullable=True))`

**Modify: `src/api/schemas/incidents.py`**
- Add `fault_type: Optional[str]` to `IncidentResponse` and `IncidentCreateRequest`

---

## Phase 2 — Adaptive Thresholding (EWMA + POT)

**New file: `src/models/ewma_tracker.py`**
- `EWMATracker(alpha=0.1, k=3.0)` — one instance per sensor per tenant
- `update(score: float) → float` — updates running mean/variance, returns current dynamic threshold
- `is_anomaly(score: float) → bool` — `score > ewma_mean + k * ewma_std`
- State serialisable to JSON for persistence in `LearningState.state_json`

**New file: `src/models/pot_threshold.py`**
- `POTThresholdFitter(q=0.90, fpr=0.001, min_peaks=10)`
- `fit(residuals: list[float]) → float` — fit GPD via `scipy.stats.genpareto`, return anomaly threshold
- `update(new_residual: float)` — maintains sliding buffer of last 500 residuals; re-fits every 50 new observations
- Fallback: `np.quantile(residuals, 1 - fpr)` when fewer than `min_peaks` peaks available

**Modify: `src/api/services/comprehensive_pipeline.py`**
- Load per-tenant `EWMATracker` and `POTThresholdFitter` from `LearningState.state_json`
- After LSTM-VAE reconstruction error is computed: update both trackers
- Use `max(ewma_threshold, pot_threshold)` as the dynamic anomaly threshold
- Fallback to fixed `ANOMALY_THRESHOLD` from `config.py` when tenant has < 50 inferences
- Persist updated tracker state back to `LearningState`

**Modify: `src/config.py`**
- Add `EWMA_ALPHA = 0.1`, `EWMA_K = 3.0`, `POT_Q = 0.90`, `POT_FPR = 0.001`, `POT_BUFFER_SIZE = 500`

---

## Phase 3 — Physics-Informed Validation Layer

**New file: `src/models/physics_validator.py`**
- `PhysicsValidator.validate(sequence: np.ndarray, timestamp: datetime) → ValidationResult`
- `ValidationResult`: `{suppressed: bool, confirmed: bool, reason: str}`

**Rules implemented:**
1. **Psychrometric impossibility**: temp trend sharply rising AND humidity simultaneously rising → physical impossibility without active misting → `confirmed=True` (real fault)
2. **Light / time-of-day**: light > 50 lux between 22:00–04:00 UTC → spike fault confirmed
3. **CO2 plausibility**: sequence mean < 200 ppm or > 2000 ppm → confirmed fault
4. **Temperature bounds**: < 5°C or > 45°C in sequence → confirmed fault

**Integration into `comprehensive_pipeline.py`:**
- Call `PhysicsValidator.validate()` on the raw (denormalized) sequence + timestamp **before** ensemble detection
- If `suppressed=True`: skip incident storage; log as "physically explained event"
- If `confirmed=True`: bypass ensemble, set `anomaly=True` directly with rule-derived fault type

---

## Phase 4 — Gas Sensor Anomaly Detection

Gas columns live in `RawReading.data` JSON (source not `synthetic:*`). LSTM-VAE cannot be used (hardcoded 4-sensor input + insufficient gas training data). Use statistical methods.

**New file: `src/models/gas_anomaly_detector.py`**
- `GasAnomalyDetector`
- `fit(readings: list[dict]) → GasBaseline` — compute per-column `{mean, std, q25, q75, iqr}` from all available readings
- `detect(values: dict[str, float], baseline: GasBaseline) → dict[str, GasAnomaly]`:
  - Z-score: `|value - mean| / std > 3.0`
  - IQR: `value < q25 - 1.5*iqr` or `value > q75 + 1.5*iqr`
  - EWMA: reuse `EWMATracker` from Phase 2 for dynamic per-column threshold
  - Returns per-column `{is_anomaly: bool, z_score: float, method: str, severity: str}`
- Gas columns handled: `c2h4`, `gas_co2`, `no`, `no2`, `nox`, `co`

**New file: `src/api/routes/gas_anomalies.py`**
- `GET /api/gas/summary?tenant_id=&greenhouse_id=&hours=24`
  - Fetches RawReading rows for gas columns within window (filters `source NOT LIKE 'synthetic:%'`)
  - Runs `GasAnomalyDetector.fit()` + `.detect()` on each reading
  - Returns: `{column, current, mean, std, anomaly_count, readings: [{timestamp, value, is_anomaly, z_score}]}`
- `GET /api/gas/readings?tenant_id=&greenhouse_id=&hours=24` — raw time-series with anomaly flags
- Register in the FastAPI app (check `src/api/main.py` for existing router registration pattern)

**New file: `src/dashboard/pages/7_gas_sensors.py`**
- Reads tenant/greenhouse from `st.session_state`
- Calls both gas API endpoints
- Combined time-series chart using `plot_combined_sensors()` from `src/dashboard/components/charts.py` — reuse directly
- Per-column metric cards: current value, status badge (Normal / Warning / Critical based on Z-score)
- Anomaly events table: timestamp, column, value, z_score, detection method
- Guard: `st.info("No gas sensor data found…")` when no rows returned

---

## Phase 5 — Dashboard: Fault Type Visibility

**Modify: `src/dashboard/components/anomaly_table.py`**
- Add `fault_type` column with colored badge to `build_html_table()`
- Badge colors: `spike_fault=red`, `calibration_drift=orange`, `stuck_fault=yellow`, `bias_offset=amber`, `accuracy_decline=grey`, `None=light-grey`

**Modify: `src/dashboard/pages/2_anomalies.py`**
- Add fault type filter dropdown in sidebar (multiselect from the 5 types + "Unknown")
- Filter incident list before rendering table

---

## Files Changed Summary

| File | Change |
|------|--------|
| `src/models/fault_classifier.py` | NEW |
| `src/models/stuck_fault_detector.py` | NEW |
| `src/models/bias_detector.py` | NEW |
| `src/models/ewma_tracker.py` | NEW |
| `src/models/pot_threshold.py` | NEW |
| `src/models/physics_validator.py` | NEW |
| `src/models/gas_anomaly_detector.py` | NEW |
| `src/api/routes/gas_anomalies.py` | NEW |
| `src/dashboard/pages/7_gas_sensors.py` | NEW |
| `alembic/versions/010_add_fault_type_to_incidents.py` | NEW |
| `src/models/ensemble_detector.py` | MODIFY — add stuck + bias pre-checks |
| `src/api/services/comprehensive_pipeline.py` | MODIFY — integrate all new components |
| `src/db/models.py` | MODIFY — add `fault_type` to Incident |
| `src/api/schemas/incidents.py` | MODIFY — add `fault_type` field |
| `src/config.py` | MODIFY — EWMA/POT params, gas column list |
| `src/dashboard/pages/2_anomalies.py` | MODIFY — fault type badge + filter |
| `src/dashboard/components/anomaly_table.py` | MODIFY — fault_type column |
| `src/api/main.py` | MODIFY — register gas route |

---

## Agent Dispatch Plan

**Parallel Phase A** (launch together):
- `ml-engineer` → Phases 1–3: all new `src/models/` files + modifications to `ensemble_detector.py`, `comprehensive_pipeline.py`, `config.py`. Run smoke tests after each component.
- `backend-engineer` → Phase 4 backend: `gas_anomaly_detector.py`, `gas_anomalies.py` route, DB migration 010, schema update, register route in main.py.

**Sequential Phase B** (after Phase A completes):
- `backend-engineer` → Phase 5: dashboard enhancements — anomaly page fault type badges + gas sensor page 7.

**Sequential Phase C**:
- `qa-engineer` → full test suite on all new files; 80% coverage floor on new `src/models/` additions.
- `code-reviewer` → security + quality review on all changed files.

---

## Key Reuse Points

- `SENSOR_RANGES` in `src/config.py` → `BiasOffsetDetector` uses these for expected midpoints
- `EWMATracker` (Phase 2) → reused by `GasAnomalyDetector` (Phase 4) for per-gas-column dynamic thresholds
- `plot_combined_sensors()` in `src/dashboard/components/charts.py` → gas dashboard page reuses directly
- `LearningState.state_json` in `src/db/models.py` → persists EWMA + POT state between API restarts
- `IncidentMemory.store_incident()` in `src/db/incident_memory.py` → extended with `fault_type` kwarg

---

## Verification

1. **Fault taxonomy**: Inject `temperature_spike` test anomaly → Anomaly page shows badge `spike_fault`. Inject `gradual_drift` → badge `calibration_drift`.
2. **Stuck fault**: Inject synthetic data with constant temperature (zero variance across all 20 timesteps) → detector flags `stuck_fault` before ensemble runs.
3. **EWMA/POT**: After 50+ inferences for a tenant, check that threshold in pipeline logs is non-constant and different from `ANOMALY_THRESHOLD` config value.
4. **Physics validation**: Feed sequence where temperature AND humidity both spike upward sharply → pipeline logs `confirmed=True`, incident stored with `fault_type=spike_fault`; no false suppression.
5. **Gas detection**: Upload `2025_1d_english.csv` (contains c2h4/no/no2/nox/co columns) → page 7 shows gas time-series with anomaly markers on statistical outliers.
6. **Tests**: `./venv/bin/pytest tests/ --cov=src --cov-fail-under=80`

# Phase 2.0 — What's Implemented

## Pass 1 — Pattern Learning

**Files:** `engines/pass1_learn.py`, `engines/pass1_seasonal_splitter.py`

**Seasonal split:** data is divided into spring (Mar–May), summer (Jun–Aug), autumn (Sep–Nov), winter (Dec–Feb). Seasons with fewer than 100 rows are dropped. One pattern model is stored per season.

**Per-column statistics learned:** mean, std, min, max, median, q25, q75.

**Thresholds derived from data (not hardcoded):**
- Spike: `threshold = σ × 2.5`
- Stuck: `threshold = σ² × 0.01`
- Drift: `threshold = σ × 3.0`
- Precision: `threshold = σ × 0.1`

**Diurnal profile:** hourly mean per sensor → peak hour, trough hour, full 24-element hourly profile.

**Correlation matrix:** Pearson correlations between all sensor pairs.

---

## Pass 2 — Anomaly Detection (5 univariate detectors)

Each detector runs row-by-row with rolling state, reset per season.

### 1. Spike — `detectors/spike.py`
- Trigger: `|value − prev_value| > threshold_spike`
- Confidence: `min(diff / (2 × threshold), 1.0)`
- Rolling buffer: last 10 values

### 2. Stuck — `detectors/stuck.py`
- Trigger: `Var(window₂₀) < threshold_stuck`
- Confidence: `1 − min(variance / threshold, 1.0)`
- Rolling window: 20 readings

### 3. Drift — `detectors/drift.py`
- CUSUM: `S_t = max(0, S_{t-1} + (value − μ) / σ)`
- Trigger: `S_t > threshold_drift`
- Confidence: `min(S_t / (2 × threshold), 1.0)`
- Resets to 0 when no deviation (one-sided CUSUM)

### 4. Precision — `detectors/precision.py`
- Trigger: `std(window₅₀) > threshold_precision`
- Confidence: `min(noise / (2 × threshold), 1.0)`
- Rolling window: 50 readings

### 5. Polling — `detectors/polling.py`
- Trigger: 5 consecutive null/NaN readings
- Confidence: `min(null_count / 10, 1.0)`
- Counter resets to 0 on any valid reading

---

## DBSCAN Clustering — `engines/anomaly_clustering.py`

Groups related anomalies into incidents. Feature vector per anomaly:

```
[unix_timestamp / 3600,   # hours — 30 min gap ≈ 0.5 units
 sensor_index × 1.0,      # different sensors always > eps apart
 confidence × 0.05]       # tiny weight, tie-break only
```

- Algorithm: `DBSCAN(eps=0.5, min_samples=1)`
- Anomalies on the same sensor within ~30 minutes cluster together; different sensors are always split
- Severity: `confidence > 0.8 → HIGH`, `> 0.5 → MEDIUM`, else `LOW`
- Output per cluster: time_start, time_end, duration_minutes, affected_sensors, likely_cause, severity, confidence

---

## Rule-Based Explainability — `models/explainability.py`

SHAP substitute — no ML. Dominant fault type by frequency among triggered detectors determines the primary cause. Contributing features = `(sensor, confidence)` pairs sorted by confidence descending.

---

## Root Cause Inference — `engines/root_cause_inference.py`

> Gap filled this session.

Confidence-weighted scoring over `(fault_type, sensor)` pairs against a rule table. For each anomaly, accumulates `score[fault_type, sensor] += confidence`. Best-scoring rule wins.

13 rules covering: stuck/drift/spike × temperature/humidity/co2/light, plus polling (any sensor) and precision (any sensor). Rule confidences: 0.72–0.90.

Output: `{incident_type, confidence, description, affected_sensors, recommendation}`.

---

## Active Learning — `models/active_learner.py`

> Gap filled this session.

`OperatorFeedbackLearner` — pure in-memory, per-customer.

**Retrain gate — all 3 conditions must be true:**
1. `len(buffer) ≥ retrain_threshold` (default: 20)
2. `|distinct reasons in buffer| ≥ 2`
3. `mean(operator_accuracy.values()) ≥ 0.80`

Operators initialise at accuracy 0.5. Buffer is cleared after trigger.

---

## Threshold Optimizer — `engines/threshold_optimizer.py`

> Gap filled this session.

Grid search over 6 × 4 × 4 = 96 parameter combinations:
- `spike_multiplier`: [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
- `stuck_variance`: [0.001, 0.005, 0.01, 0.05]
- `drift_cusum`: [0.02, 0.05, 0.1, 0.15]

Objective — F1-score from operator feedback labels:

```
precision = TP / (TP + FP)
recall    = TP / (TP + FN + 1)    # +1 smoothing prevents divide-by-zero
F1        = 2 × precision × recall / (precision + recall)
```

Best `(spike_multiplier, stuck_variance, drift_cusum)` returned. Called automatically when retraining is triggered.

---

## Feedback API — `api/routes/feedback.py`

> Gap filled this session.

| Endpoint | Description |
|----------|-------------|
| `POST /api/feedback/mark-anomaly` | JSON body, returns `learning_triggered` + `progress_to_retrain` |
| `GET /api/feedback/statistics/{customer_id}` | Buffer stats per customer |
| `GET /api/feedback/learning-progress/{customer_id}` | `progress_pct`, `ready_to_retrain` |

Persists every verdict to `operator_feedback` DB table. Threshold optimizer fires on retrain trigger.

---

## Dual-Detector Merge — `src/api/services/cold_run_upload.py`

> Added this session.

Both LSTM and rolling z-score statistical detectors run on every uploaded CSV. Results merged by timestamp:

| Case | `detector` | `agreement` | Statistical fields |
|------|-----------|-------------|-------------------|
| Both flagged same timestamp | `lstm+statistical` | `True` | filled |
| LSTM only | `lstm` | `False` | empty |
| Statistical only | `statistical` | `False` | filled |

Score on merged rows = `max(lstm_score, stat_score)`.
Statistical filter: `severity ∈ {warning, critical}` (was critical-only before).

---

## Jump-to-Monitoring UI

> Added this session.

`⬡` button per anomaly row in the Upload & Find Anomalies results table.

**On click:**
1. Sets `time_start` / `time_end` to the anomaly's calendar day (00:00–23:59)
2. Pre-selects sensors: specific column for statistical-only detections; all 4 sensors for LSTM or agreement rows
3. Stores anomaly highlight dict in session state
4. Navigates to the Monitoring page via `st.switch_page`

**On the Monitoring page:**
- LSTM anomalies: shaded band covering the 20-step sequence window + dashed vertical line
- Statistical anomalies: dot marker on the specific sensor trace at the exact normalized value
- Agreement (`lstm+statistical`): both overlaid, coloured red
- LSTM-only: orange; statistical-only: gold

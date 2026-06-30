# Two-Pass Cold-Start Anomaly Detection — Bootstrap Pipeline Architecture

**Date:** 2026-06-25
**Author:** Architect Agent
**Status:** Design Complete — ready for implementation

---

## 1. Problem Statement

When a new greenhouse tenant uploads their first CSV of historical data, the platform has no trained LSTM-VAE model, no learned baseline patterns, and no adaptive thresholds. The existing cold-run detector (`src/models/cold_run_detector.py`) applies rolling z-scores to individual columns, but it cannot leverage psychrometric physics or isolate training-quality clean data from anomalous rows.

The Two-Pass Bootstrap pipeline solves this by:
1. **Pass 1** — Using physics-aware heuristics (psychrometric humidity deficit, Isolation Forest) to separate clean data from suspected anomalies, producing a `bootstrapped_clean.csv` and a `pass1_suspected_anomalies.csv`.
2. **Pass 2** — Training an LSTM-VAE on the clean subset, running full inference on the original data, and applying univariate fault classifiers to produce a final anomaly report.

---

## 2. Data Flow Diagram

```
  ┌──────────────────────────────────────────────────────────────────────────┐
  │                     POST /api/v1/bootstrap/run                         │
  │                     (UploadFile + tenant_id + greenhouse_id)           │
  └────────────────────────────────┬───────────────────────────────────────┘
                                   │
                                   ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  STEP 0: CSV Ingest & Validate                                        │
  │  ─────────────────────────────────                                     │
  │  CSVAdapter.normalize(file)                                            │
  │  → Resample to BOOTSTRAP_RESAMPLE_INTERVAL ('5min')                    │
  │  → Validate: ≥4 columns (temp, humidity, co2, light)                   │
  │  → 422 if CSV unparseable; 400 if <100 clean rows after null drop      │
  └────────────────────────────────┬───────────────────────────────────────┘
                                   │
                                   ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  PASS 1: Physics + Isolation Forest Cleaning                           │
  │  ═══════════════════════════════════════                                │
  │                                                                        │
  │  1a. Gas Column Null Audit                                             │
  │      ── drop columns where null_fraction > BOOTSTRAP_GAS_NULL_THRESHOLD│
  │         (0.95) — these are unconnected gas sensors                     │
  │                                                                        │
  │  1b. Psychrometric Humidity Deficit (HD) Check                         │
  │      ── compute HD = f(temp, humidity) per row                         │
  │      ── flag rows where |HD_actual - HD_expected| > HD_TOLERANCE (0.05)│
  │      ── uses Magnus formula for saturation vapor pressure              │
  │                                                                        │
  │  1c. Spike-Delta Pre-filter                                            │
  │      ── flag rows where |Δx_t| > SPIKE_DELTA_MAX per sensor            │
  │         temp: 5.0°C, hum: 10.0%, co2: 50.0ppm, light: 200.0lux       │
  │                                                                        │
  │  1d. Isolation Forest (sklearn)                                        │
  │      ── fit on 4 core sensors (temp, humidity, co2, light)             │
  │      ── contamination = BOOTSTRAP_IF_CONTAMINATION (0.04)              │
  │      ── rows scored -1 (outlier) are flagged                           │
  │                                                                        │
  │  1e. Row Classification                                                │
  │      ── CLEAN: not flagged by any of 1b/1c/1d                          │
  │      ── SUSPECT: flagged by ≥1 detector                                │
  │      ── Write clean → BOOTSTRAP_CLEAN_PATH                             │
  │      ── Write suspect → BOOTSTRAP_ANOMALY_PATH                         │
  │      ── 400 if clean row count < 100                                   │
  └────────────────────────────────┬───────────────────────────────────────┘
                                   │
                                   ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  PASS 2: LSTM-VAE Train + Full Inference                               │
  │  ═══════════════════════════════════════                                │
  │                                                                        │
  │  2a. LSTM-VAE Training                                                 │
  │      ── Load bootstrapped_clean.csv                                    │
  │      ── Normalize with SENSOR_RANGES (existing src/models/data_utils)  │
  │      ── Window into sequences of SEQ_LEN=20                            │
  │      ── Train LSTM-VAE (MODEL_CONFIG from src/config.py)               │
  │      ── Save checkpoint to PATHS["checkpoint_dir"]                     │
  │                                                                        │
  │  2b. Full-File Inference                                               │
  │      ── Run trained model on ALL original rows (not just clean)        │
  │      ── Produce per-sequence reconstruction error scores               │
  │                                                                        │
  │  2c. Threshold Calibration                                             │
  │      ── Compute threshold at BOOTSTRAP_THRESHOLD_PERCENTILE (99.0)     │
  │      ── of the clean-subset scores (not full-file scores)              │
  │                                                                        │
  │  2d. Univariate Fault Classifiers                                      │
  │      ── For each sequence above threshold:                             │
  │         ── Run spike detector (pattern_scores.score_spike)             │
  │         ── Run stuck-fault detector (stuck_fault_detector)             │
  │         ── Run dropout detector (dropout_detector)                     │
  │         ── Run CUSUM drift detector (cusum_drift_detector)             │
  │         ── Run noise/precision detector (noise_detector)               │
  │         ── Classify fault type via FaultClassifier                     │
  └────────────────────────────────┬───────────────────────────────────────┘
                                   │
                                   ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  STEP 3: Assemble Response                                             │
  │  ─────────────────────────────                                         │
  │  {                                                                     │
  │    "status": "complete",                                               │
  │    "pass1_summary": {                                                  │
  │      "total_rows": N,                                                  │
  │      "clean_rows": M,                                                  │
  │      "suspected_rows": K,                                              │
  │      "dropped_gas_columns": [...],                                     │
  │      "clean_csv_path": "data/bootstrapped_clean.csv",                  │
  │      "anomaly_csv_path": "data/pass1_suspected_anomalies.csv"          │
  │    },                                                                  │
  │    "pass2_summary": {                                                  │
  │      "model_checkpoint": "models/checkpoints/bootstrap_<tenant>.pt",   │
  │      "threshold": 0.042,                                               │
  │      "threshold_method": "percentile_99",                              │
  │      "anomalies_detected": 17,                                         │
  │      "anomalies": [                                                    │
  │        {                                                               │
  │          "timestamp": "2025-01-15T14:30:00",                           │
  │          "score": 0.087,                                               │
  │          "fault_type": "spike_fault",                                  │
  │          "affected_sensors": ["temperature"],                          │
  │          "confidence": 0.92                                            │
  │        }, ...                                                          │
  │      ]                                                                 │
  │    }                                                                   │
  │  }                                                                     │
  └────────────────────────────────────────────────────────────────────────┘
```

---

## 3. New Files & Import Relationships

### 3.1 File Manifest

| File | Responsibility | Lines (est.) |
|------|---------------|-------------|
| `src/api/routes/bootstrap.py` | FastAPI route: `POST /api/v1/bootstrap/run` | ~60 |
| `src/api/services/bootstrap_pipeline.py` | Orchestrates Pass 1 → Pass 2 → response assembly | ~120 |
| `src/api/schemas/bootstrap.py` | Pydantic request/response models | ~80 |
| `src/models/psychrometric_validator.py` | Humidity deficit calculator (Magnus formula) | ~60 |
| `src/models/bootstrap_cleaner.py` | Pass 1 logic: physics + IF → clean/suspect split | ~100 |
| `src/models/bootstrap_trainer.py` | Pass 2 logic: LSTM-VAE train + inference + classify | ~100 |

### 3.2 Import Dependency Graph

```
src/api/routes/bootstrap.py
├── src/api/schemas/bootstrap.py             (request/response models)
├── src/api/services/bootstrap_pipeline.py   (pipeline orchestrator)
└── src/db/session.py                        (get_db)

src/api/services/bootstrap_pipeline.py
├── src/models/bootstrap_cleaner.py          (Pass 1)
├── src/models/bootstrap_trainer.py          (Pass 2)
├── src/vendors/csv_adapter.py               (CSVAdapter.normalize)
└── src/config.py                            (BOOTSTRAP_* constants)

src/models/bootstrap_cleaner.py
├── src/models/psychrometric_validator.py    (HD check)
├── src/config.py                            (BOOTSTRAP_*, SPIKE_DELTA_MAX, SENSOR_NAMES)
├── sklearn.ensemble.IsolationForest         (external — already in requirements)
└── pandas / numpy                           (external)

src/models/bootstrap_trainer.py
├── src/models/lstm_vae.py                   (LSTMVAE model class)
├── src/models/training.py                   (train_model or training loop)
├── src/models/data_utils.py                 (normalize_data, preprocess_sequences, SENSOR_RANGES)
├── src/models/inference.py                  (AnomalyDetector.predict_batch_enriched)
├── src/models/fault_classifier.py           (FaultClassifier.classify)
├── src/models/pattern_scores.py             (score_spike)
├── src/models/stuck_fault_detector.py       (StuckFaultDetector)
├── src/models/dropout_detector.py           (DropoutDetector)
├── src/models/cusum_drift_detector.py       (CUSUMDriftDetector)
├── src/models/noise_detector.py             (NoiseDetector)
├── src/models/cold_run_threshold.py         (calibrate_threshold — reuse percentile logic)
└── src/config.py                            (MODEL_CONFIG, TRAINING_CONFIG, BOOTSTRAP_*)

src/models/psychrometric_validator.py
├── numpy                                    (external)
└── src/config.py                            (PSYCHROMETRIC_HD_TOLERANCE)

src/api/schemas/bootstrap.py
└── pydantic                                 (external — BaseModel, Field)
```

### 3.3 Circular Dependency Verification

**No circular dependencies introduced.** The dependency flow is strictly:

```
route → service → models → config
                → vendors → config
```

- `bootstrap_cleaner.py` and `bootstrap_trainer.py` are both in `src/models/` but have **no cross-imports** between them. The service layer (`bootstrap_pipeline.py`) calls them sequentially.
- `psychrometric_validator.py` is imported only by `bootstrap_cleaner.py` — no back-references.
- All new files import from existing `src/models/` modules (downstream) and `src/config.py` (leaf). None import from `src/api/` (upstream), preserving the one-directional dependency flow.

---

## 4. Config Additions for `src/config.py`

Add the following section after the existing "Gas Sensor Detection (SCADA — Phase 4)" block:

```
# =============================================================================
# Cold-Start Bootstrap Pipeline (Two-Pass)
# =============================================================================

# Pass 1: columns with null fraction above this are dropped (unconnected gas sensors).
BOOTSTRAP_GAS_NULL_THRESHOLD: float = 0.95

# Pass 1: Isolation Forest contamination parameter (expected anomaly fraction).
BOOTSTRAP_IF_CONTAMINATION: float = 0.04

# Pass 1: resample interval for regularizing irregular timestamps.
BOOTSTRAP_RESAMPLE_INTERVAL: str = '5min'

# Pass 1: output path for the physics-validated clean subset.
BOOTSTRAP_CLEAN_PATH: str = 'data/bootstrapped_clean.csv'

# Pass 1: output path for rows flagged as suspected anomalies.
BOOTSTRAP_ANOMALY_PATH: str = 'data/pass1_suspected_anomalies.csv'

# Pass 2: percentile of clean-subset scores used as detection threshold.
BOOTSTRAP_THRESHOLD_PERCENTILE: float = 99.0

# Psychrometric humidity deficit tolerance (fraction).
# Rows where |HD_actual - HD_expected| > this value are flagged.
PSYCHROMETRIC_HD_TOLERANCE: float = 0.05

# Maximum per-step delta before a row is flagged as a spike in Pass 1.
# Keys must match SENSOR_NAMES; values are in physical sensor units.
SPIKE_DELTA_MAX: dict = {
    'temperature': 5.0,   # °C
    'humidity': 10.0,      # %
    'co2': 50.0,           # ppm
    'light': 200.0,        # lux
}
```

**Placement:** After the `SENSOR_NOISE_TARGETS` block (line ~625) and before the `AUTH_ENABLED` block.

**No conflicts with existing constants:** `SPIKE_DELTA_MAX` is distinct from the existing `VELOCITY_LIMITS` (used by the spike fault detector in the SCADA real-time pipeline; units are per-timestep velocity, not per-step delta in raw upload data). Both are needed — `VELOCITY_LIMITS` operates on normalized 20-step sequences, `SPIKE_DELTA_MAX` operates on raw CSV rows at 5-minute intervals.

---

## 5. API Route Contract

### `POST /api/v1/bootstrap/run`

**Router prefix:** `/api/v1/bootstrap`
**Tag:** `bootstrap`

This follows the existing FastAPI patterns observed in `src/api/routes/upload.py` and `src/api/routes/inference.py`:
- `APIRouter()` with explicit prefix
- `UploadFile = File(...)` for CSV
- `Form(...)` for `tenant_id` and `greenhouse_id`
- `AsyncSession = Depends(get_db)` for database access
- Standard error raising via `HTTPException`

#### Request

| Parameter | Type | Source | Required |
|-----------|------|--------|----------|
| `file` | `UploadFile` | `File(...)` | Yes |
| `tenant_id` | `str` | `Form(...)` | Yes |
| `greenhouse_id` | `str` | `Form(...)` | Yes |

#### Response (200 OK)

```json
{
  "status": "complete",
  "tenant_id": "tenant_abc",
  "greenhouse_id": "gh_01",
  "pass1_summary": {
    "total_rows": 2880,
    "clean_rows": 2754,
    "suspected_rows": 126,
    "dropped_gas_columns": ["no2", "nox"],
    "clean_csv_path": "data/bootstrapped_clean.csv",
    "anomaly_csv_path": "data/pass1_suspected_anomalies.csv",
    "detectors_fired": {
      "psychrometric_hd": 42,
      "spike_delta": 31,
      "isolation_forest": 83
    }
  },
  "pass2_summary": {
    "model_checkpoint": "models/checkpoints/bootstrap_tenant_abc.pt",
    "threshold": 0.042,
    "threshold_method": "percentile_99",
    "total_sequences": 2860,
    "anomalies_detected": 17,
    "anomalies": [
      {
        "timestamp": "2025-01-15T14:30:00",
        "score": 0.087,
        "fault_type": "spike_fault",
        "affected_sensors": ["temperature"],
        "confidence": 0.92
      }
    ]
  }
}
```

#### Pydantic Schema Breakdown

| Schema | Purpose |
|--------|---------|
| `BootstrapPass1Summary` | Pass 1 statistics: row counts, dropped columns, detector fire counts |
| `BootstrapAnomalyDetail` | Single detected anomaly: timestamp, score, fault_type, sensors, confidence |
| `BootstrapPass2Summary` | Pass 2 statistics: checkpoint path, threshold, anomaly list |
| `BootstrapResponse` | Top-level response wrapping both summaries |

---

## 6. Error Handling Contract

| HTTP Code | Condition | Detail Message Pattern |
|-----------|-----------|----------------------|
| **422** | CSV cannot be parsed by `CSVAdapter.normalize()` — missing required columns, unparseable timestamps, corrupt file | `"CSV validation failed: {specific_reason}"` |
| **422** | File is not a CSV (wrong content type or extension) | `"Expected CSV file, got {content_type}"` |
| **400** | After Pass 1 cleaning, fewer than 100 clean rows remain — insufficient data for LSTM-VAE training | `"Insufficient clean data for training: {n} clean rows (minimum 100 required)"` |
| **400** | `tenant_id` or `greenhouse_id` missing/empty | `"tenant_id and greenhouse_id are required"` |
| **500** | LSTM-VAE training fails (convergence error, CUDA OOM, etc.) | `"Bootstrap training failed: {error}"` — logged with full traceback server-side |
| **200** | Pipeline completed successfully | Full `BootstrapResponse` JSON |

**Error handling implementation notes:**
- The route handler (`bootstrap.py`) catches `NormalizationError` and `DataValidationError` from `CSVAdapter` and re-raises as `HTTPException(422)`.
- The service layer (`bootstrap_pipeline.py`) raises `ValueError` for the <100 clean rows case; the route converts this to `HTTPException(400)`.
- Training failures are caught at the service layer, logged with `logger.error(..., exc_info=True)`, and re-raised as `RuntimeError`; the route converts to `HTTPException(500)`.
- All errors include a `request_id` via the existing middleware in `src/api/main.py`.

---

## 7. Reuse Map

### Modules reused from existing codebase

| New File | Imports From | What It Uses |
|----------|-------------|--------------|
| `bootstrap_pipeline.py` | `src/vendors/csv_adapter.CSVAdapter` | `normalize()` for column mapping, timestamp parsing, NaN handling |
| `bootstrap_pipeline.py` | `src/config.py` | All `BOOTSTRAP_*` constants, `SENSOR_NAMES`, `MODEL_CONFIG`, `TRAINING_CONFIG` |
| `bootstrap_cleaner.py` | `src/config.py` | `BOOTSTRAP_GAS_NULL_THRESHOLD`, `BOOTSTRAP_IF_CONTAMINATION`, `SPIKE_DELTA_MAX`, `SENSOR_NAMES` |
| `bootstrap_cleaner.py` | `sklearn.ensemble.IsolationForest` | Already in `requirements.txt` (used by `engines/` and ensemble detector) |
| `bootstrap_trainer.py` | `src/models/data_utils.normalize_data` | Min-max normalization using `SENSOR_RANGES` |
| `bootstrap_trainer.py` | `src/models/data_utils.preprocess_sequences` | Windowing into `(batch, SEQ_LEN, INPUT_DIM)` sequences |
| `bootstrap_trainer.py` | `src/models/lstm_vae.LSTMVAE` | Model class instantiation from `MODEL_CONFIG` |
| `bootstrap_trainer.py` | `src/models/training.train_model` | Training loop (or equivalent — wraps optimizer, loss, early stopping) |
| `bootstrap_trainer.py` | `src/models/inference.AnomalyDetector` | `predict_batch_enriched()` for reconstruction error scores |
| `bootstrap_trainer.py` | `src/models/cold_run_threshold.calibrate_threshold` | Percentile-based threshold calibration (reuse, not rewrite) |
| `bootstrap_trainer.py` | `src/models/fault_classifier.FaultClassifier` | `classify()` for assigning fault types to detected anomalies |
| `bootstrap_trainer.py` | `src/models/pattern_scores.score_spike` | Spike detection on flagged sequences |
| `bootstrap_trainer.py` | `src/models/stuck_fault_detector.StuckFaultDetector` | Stuck-sensor detection on flagged sequences |
| `bootstrap_trainer.py` | `src/models/dropout_detector.DropoutDetector` | Null/NaN dropout detection |
| `bootstrap_trainer.py` | `src/models/cusum_drift_detector.CUSUMDriftDetector` | Drift detection (stateless per-sequence, no persistence needed at bootstrap) |
| `bootstrap_trainer.py` | `src/models/noise_detector.NoiseDetector` | Precision degradation on flagged sequences |
| `bootstrap.py` (route) | `src/db/session.get_db` | Async DB session dependency injection |
| `bootstrap.py` (route) | `src/vendors/csv_adapter.NormalizationError` | Exception class for 422 handling |

### Modules NOT reused (and why)

| Module | Reason Not Reused |
|--------|-------------------|
| `src/api/services/comprehensive_pipeline.py` | Too tightly coupled to real-time single-sequence inference; carries EWMA/POT state, incident storage, active learning. Bootstrap is batch-mode, stateless. |
| `src/models/physics_validator.py` | Operates on normalized 20-step sequences with time-of-day rules. Bootstrap Pass 1 needs row-level psychrometric HD checks on raw physical-unit data. Different granularity and different physics. The new `psychrometric_validator.py` is complementary, not duplicative. |
| `engines/pass1_learn.py` (root-level Phase 2) | Different codebase lineage (root-level `engines/` vs `src/`). Uses `core.config` imports, not `src.config`. Pattern learning is for seasonal baseline detection, not for clean/dirty row separation. |
| `src/models/ensemble_detector.py` | Ensemble requires a fitted LSTM-VAE baseline. At bootstrap time, we are training the LSTM-VAE — the ensemble is downstream, not applicable during cleaning. |

---

## 8. Sequence of Operations (Implementation Order)

```
Phase 1 — Config + Schemas (no dependencies)
  1. Add BOOTSTRAP_* constants to src/config.py
  2. Create src/api/schemas/bootstrap.py

Phase 2 — Pass 1 Models (depends on config)
  3. Create src/models/psychrometric_validator.py
  4. Create src/models/bootstrap_cleaner.py

Phase 3 — Pass 2 Models (depends on config, existing models)
  5. Create src/models/bootstrap_trainer.py

Phase 4 — Service + Route (depends on all above)
  6. Create src/api/services/bootstrap_pipeline.py
  7. Create src/api/routes/bootstrap.py
  8. Register router in src/api/main.py

Phase 5 — Tests + Review
  9. QA: unit tests for psychrometric_validator, bootstrap_cleaner, bootstrap_trainer
  10. QA: integration test for POST /api/v1/bootstrap/run
  11. Code review
```

---

## 9. Router Registration

Add to `src/api/main.py` imports:

```
from src.api.routes import bootstrap
```

And in the router registration block (after the existing `from src.api.routes import patterns` if present, or after `monitoring`):

```
app.include_router(bootstrap.router)
```

The route prefix `/api/v1/bootstrap` does not conflict with any existing prefix:
- `/api/inference/` — inference routes
- `/api/upload/` — CSV upload routes
- `/api/gas/` — gas sensor routes
- `/api/anomaly/` — root-level Phase 2 anomaly routes
- `/api/v1/bootstrap/` — **new** (versioned namespace for bootstrap)

---

## 10. Open Design Questions (for implementer)

1. **Tenant-scoped output paths:** The current `BOOTSTRAP_CLEAN_PATH` and `BOOTSTRAP_ANOMALY_PATH` are global. For multi-tenant, the service should append `/{tenant_id}/{greenhouse_id}/` to isolate outputs. The config values serve as base path templates.

2. **Training duration:** LSTM-VAE training on 2,000–10,000 clean rows may take 30–120 seconds on CPU. The endpoint should remain synchronous for Phase 1 (files <50k rows). A background-task variant can be added later if needed.

3. **Checkpoint naming:** Use `bootstrap_{tenant_id}.pt` to avoid collisions with active-learning retrained models (`retrain_{tenant_id}_{timestamp}.pt`).

4. **Idempotency:** Re-running bootstrap for the same tenant should overwrite the previous checkpoint and clean CSV, not accumulate. The service should delete prior bootstrap artifacts before writing new ones.

# Two-Pass Cold-Start Bootstrap — Code Review

**Reviewer:** code-reviewer (Claude Opus)
**Date:** 2026-06-25
**Verdict:** **NEEDS FIXES** (1 HIGH, 2 MEDIUM-HIGH, 4 MEDIUM, 5 LOW/NOTE)

## Files Reviewed

| File | Lines | Role |
|------|-------|------|
| `src/models/psychrometric_validator.py` | 113 | Tetens equation + HD validation |
| `engines/cold_start_bootstrap.py` | 336 | Pass 1 pipeline (physics + IF) |
| `engines/pass2_bootstrap_detect.py` | 524 | Pass 2 pipeline (LSTM-VAE + 5 classifiers) |
| `src/api/routes/bootstrap.py` | 176 | FastAPI route for bootstrap |
| `src/api/schemas/bootstrap.py` | 30 | Pydantic response DTOs |
| `src/config.py` | 664 | All bootstrap/psychrometric/spike config |
| `src/models/cusum_drift_detector.py` | 150 | CUSUM drift detector |
| `src/models/stuck_fault_detector.py` | 79 | Stuck-fault detector |
| `src/models/noise_detector.py` | 60 | Precision degradation detector |
| `src/models/dropout_detector.py` | 63 | Polling dropout detector |

---

## 1. PSYCHROMETRIC MATH

**Status: PASS**

The Tetens equation implementation in `psychrometric_validator.py` exactly matches the spec:

| Spec | Implementation (line) | Match |
|------|----------------------|-------|
| `e_s = 0.61078 * exp(17.27 * T / (T + 237.3))` | L54: `0.61078 * math.exp((17.27 * t_air) / (t_air + 237.3))` | Exact |
| `HD = 216.7 * (e_s - e_a) / (T + 273.15)` | L80: `(216.7 * (e_s - e_a)) / t_kelvin` | Exact |
| `e_a = e_s * (RH / 100)` | L77: `e_a = e_s * (rh / 100.0)` | Exact |
| `\|HD_meas - HD_calc\| > 0.05` | L109: `abs(hd_measured - hd_calc) > PSYCHROMETRIC_HD_TOLERANCE` | Exact |

Tolerance sourced from `src.config.PSYCHROMETRIC_HD_TOLERANCE = 0.05` (config.py L648). No hardcoded coefficients in the comparison logic.

**Guard clauses:**
- Absolute-zero guard: `t_air <= -273.15` raises `ValueError` (L26, L49).
- RH range guard: `rh < 0.0 or rh > 100.0` raises `ValueError` (L31-34).
- Both checked at entry to `calculate_humidity_deficit` via `_validate_inputs` (L74).

No issues found.

---

## 2. HARDCODED NUMBERS AUDIT

**Status: NEEDS FIXES** (4 findings)

### Config-sourced thresholds (all correct):

| Threshold | Config constant | Usage location | Status |
|-----------|----------------|----------------|--------|
| IF contamination 0.04 | `BOOTSTRAP_IF_CONTAMINATION` | cold_start_bootstrap.py L297 | PASS |
| Gas null 0.95 | `BOOTSTRAP_GAS_NULL_THRESHOLD` | cold_start_bootstrap.py L208 | PASS |
| Resample 5min | `BOOTSTRAP_RESAMPLE_INTERVAL` | cold_start_bootstrap.py L160 | PASS |
| Threshold P99 | `BOOTSTRAP_THRESHOLD_PERCENTILE` | pass2_bootstrap_detect.py L156 | PASS |
| SPIKE_DELTA_MAX | `SPIKE_DELTA_MAX` dict | pass2_bootstrap_detect.py L394 | PASS |
| HD tolerance 0.05 | `PSYCHROMETRIC_HD_TOLERANCE` | psychrometric_validator.py L109 | PASS |
| CUSUM K/H | `CUSUM_K_SIGMA`/`CUSUM_H_SIGMA` | cusum_drift_detector.py L48 | PASS |
| Stuck window/threshold | `STUCK_WINDOW_K`/`STUCK_VARIANCE_THRESHOLD` | pass2_bootstrap_detect.py L37, stuck_fault_detector.py L36 | PASS |
| Noise alpha | `NOISE_ALPHA` | noise_detector.py L17 | PASS |
| Dropout P | `DROPOUT_CONSECUTIVE_P` | pass2_bootstrap_detect.py L38, dropout_detector.py L27 | PASS |

### Hardcoded values NOT in config:

| ID | File | Line | Value | Description | Severity |
|----|------|------|-------|-------------|----------|
| HC-1 | cold_start_bootstrap.py | L117 | `100` | Minimum clean rows after Pass 1 filtering | MEDIUM |
| HC-2 | cold_start_bootstrap.py | L290 | `10` | Minimum valid rows for IsolationForest fitting | MEDIUM |
| HC-3 | cold_start_bootstrap.py | L192 | `0.5` | NaT ratio threshold for timestamp format fallback | LOW |
| HC-4 | cold_start_bootstrap.py | L298 | `42` | IsolationForest random_state (should use `RANDOM_SEED` from config) | LOW |

HC-1 is referenced in the API route error contract (bootstrap.py L103: `"Insufficient clean rows"` check) and the architecture doc (error contract: 400 for <100 rows). Should be `BOOTSTRAP_MIN_CLEAN_ROWS` in config.

HC-4 has `RANDOM_SEED = 42` in config.py L42 but it is not imported — the value is hardcoded directly.

---

## 3. SECURITY

**Status: PASS WITH NOTES** (2 findings)

### 3.1 File path handling — PASS

- **Safe tempfile creation:** `tempfile.NamedTemporaryFile(delete=False, suffix=".csv")` (bootstrap.py L78). OS generates the filename — no user input in the path. No path traversal risk.
- **Tempfile cleanup:** `finally` block at L172-175 calls `os.unlink(tmp_path)` after checking `os.path.exists(tmp_path)`. Cleanup executes on all code paths (success, HTTP errors, unexpected exceptions).
- **Upload streaming:** Reads in 1 MB chunks (`1024 * 1024`, L79) — prevents memory exhaustion from large uploads.

### 3.2 Exception message leakage — WARN

| ID | File | Line | Detail |
|----|------|------|--------|
| SEC-1 | bootstrap.py | L84 | `detail=f"Failed to write uploaded file: {exc}"` — leaks raw OS exception (could reveal filesystem paths like `/tmp/tmpXXXXXX.csv`, mount points, or permission errors) |
| SEC-2 | bootstrap.py | L121 | `detail=f"Model training failed: {exc}"` — leaks raw PyTorch/NumPy exception messages (could reveal internal architecture details, tensor shapes, memory state) |

**Recommendation:** Replace with generic messages; log the full exception server-side:
```python
logger.error("Upload write failed: %s", exc, exc_info=True)
raise HTTPException(status_code=500, detail="Failed to process uploaded file")
```

### 3.3 No injection vectors — PASS

- No `eval()`, `exec()`, `subprocess`, or `os.system` calls.
- No SQL queries (SQLAlchemy/ORM not used in these modules).
- No `dangerouslySetInnerHTML` or `innerHTML`.
- No hardcoded secrets, tokens, or API keys.

### 3.4 Input validation — PASS

- `file: UploadFile = File(...)` and `tenant_id: str = Form(...)` — FastAPI auto-validates presence (missing → 422).
- CSVAdapter validates file existence and readability.
- Numeric bounds validated by psychrometric validator guards.

---

## 4. ERROR HANDLING

**Status: PASS**

| Error condition | Expected code | Actual code | Line | Status |
|----------------|---------------|-------------|------|--------|
| `FileNotFoundError` (CSV not found) | 422 | 422 | bootstrap.py L96-99 | PASS |
| Insufficient clean rows (<100) | 400 | 400 | bootstrap.py L100-103 | PASS |
| Model training failure | 500 | 500 | bootstrap.py L117-122 | PASS |
| Missing file/tenant_id | 422 (FastAPI) | 422 (FastAPI) | bootstrap.py L54-56 | PASS |
| Generic Pass 1 error | 500 | 500 | bootstrap.py L107-110 | PASS |
| Upload write failure | 500 | 500 | bootstrap.py L82-86 | PASS |

**Correct exception chaining:** All `raise HTTPException(...) from exc` preserve the original traceback for server-side logging while returning clean HTTP errors to clients.

**Async safety:** All blocking calls (sklearn, PyTorch) wrapped in `asyncio.to_thread()` (L93, L117, L131-132). Event loop never blocked.

**Minor note:** The ValueError match at L102 uses `msg.startswith("Insufficient clean rows")` — string-prefix matching is fragile. If the error message wording changes in `cold_start_bootstrap.py`, this would silently fall through to the generic 500 handler instead of returning 400.

---

## 5. GUARDRAIL COMPLIANCE

**Status: PASS**

### Red Flags (.claude/guardrails/red-flags.md) — ALL CLEAR

| Check | Status |
|-------|--------|
| File writes outside ownership boundaries | PASS — engines write to `data/`, API writes to tempdir |
| `os.system`, `subprocess` with `shell=True`, `eval()` | PASS — none found |
| Hardcoded passwords, tokens, API keys | PASS — none found |
| DELETE/DROP without tenant_id filter | PASS — no DB queries in reviewed files |
| `model.save()` without metadata JSON | PASS — model not persisted to disk |
| Test importing and modifying implementation files | N/A — no test files found |
| Circular imports `src/models/` ↔ `src/api/` | PASS — import flow: `routes → engines → models → config` (unidirectional) |

### Universal Guardrails (.claude/guardrails/universal-guardrails.md) — ALL CLEAR

| Check | Status |
|-------|--------|
| Tight coupling LSTM-VAE ↔ API | PASS — API imports engines, engines import models. No direct LSTM-VAE import in route. |
| Secrets in source | PASS |
| Checkpoint without metadata | PASS — no checkpoint persisted |
| DB schema without tenant_id | N/A — no DB schema changes |
| Test coverage <80% critical paths | **WARN** — zero test files found (see Section 6.1) |

### ML Guardrails (.claude/guardrails/ml-guardrails.md) — ALL CLEAR

| Check | Status |
|-------|--------|
| LSTM-VAE spec match | PASS — `LSTMVAE(input_dim=4, hidden_dims=[64,32], latent_dim=8, seq_len=20, beta=0.1, dropout=0.2)` at pass2_bootstrap_detect.py L141-148 |
| NaN/Inf detection | PASS — NaN handled via `np.isnan` checks throughout; column means used for fill |
| Hard-coded sensor ranges | PASS — all from config |
| Beta > 1.0 | PASS — `BETA = 0.1` |
| Dropout > 0.5 | PASS — `DROPOUT_RATE = 0.2` |
| Learning rate > 1e-2 | PASS — `LEARNING_RATE = 1e-3` |

---

## 6. SCADA SPEC ALIGNMENT — 5 Univariate Classifiers

**Status: PASS WITH NOTES** (2 findings)

### 6.1 Spike (Classifier 1)
**File:** pass2_bootstrap_detect.py L388-411
**Spec:** velocity > SPIKE_DELTA_MAX AND sign reversal

```python
# L405-408
delta_in = x_curr - x_prev
delta_out = x_next - x_curr
if abs(delta_in) > delta_max and _sign_differs(delta_in, delta_out):
```

- Velocity check uses `abs(delta_in) > delta_max` — correct.
- Sign reversal via `_sign_differs(a, b)` (L510-514): returns `True` when `a > 0 != b > 0` and both non-zero — correct.
- Per-sensor `delta_max` from `SPIKE_DELTA_MAX` dict — correct.
- Boundary guard: skips rows at index 0 and last row (L390-391) — correct (needs neighbors).

**Status: PASS**

### 6.2 Stuck (Classifier 2)
**File:** pass2_bootstrap_detect.py L413-433
**Spec:** rolling variance < epsilon (STUCK_VARIANCE_THRESHOLD)

- Window: `raw_values[max(0, row_idx - STUCK_WINDOW_K + 1) : row_idx + 1]` — correct K-step window.
- Delegates to `StuckFaultDetector.detect(window)` which:
  - Computes rolling variance with `pd.Series.rolling(window=STUCK_WINDOW_K).var().min()` — uses minimum over rolling windows (catches partial-stuck).
  - Also checks `value_range < STUCK_FLAT_RANGE_THRESHOLD` — dual condition.
  - Thresholds from config: `STUCK_WINDOW_K=10`, `STUCK_VARIANCE_THRESHOLD=1e-4`, `STUCK_FLAT_RANGE_THRESHOLD=1e-4`.

**SCADA-1 (NOTE):** `STUCK_FLAT_RANGE_THRESHOLD` and `STUCK_VARIANCE_THRESHOLD` are documented as "normalized-space" values (config.py L609: "Normalized-space range below which a window is treated as truly flat"). However, in `pass2_bootstrap_detect.py`, `raw_values` are in **physical units** (°C, %, ppm, lux), not normalized. A range threshold of 1e-4 in raw physical units is extremely tight but still functionally correct for detecting truly frozen sensors (readings identical to 4+ decimal places). The threshold is valid but the config comment is misleading.

**Status: PASS WITH NOTES**

### 6.3 Drift (Classifier 3)
**File:** pass2_bootstrap_detect.py L434-457
**Spec:** two-sided CUSUM with K and H from config

- Standardized residual: `(x_t - baseline_mean) / baseline_std` (L448-450) — correct.
- Delegates to `CUSUMDriftDetector.update(sensor, residual)` (L454).
- CUSUM implementation (cusum_drift_detector.py L80-86):
  ```python
  S+ = max(0, S+ + residual - k)
  S- = max(0, S- - residual - k)
  alarm = S+ > h or S- > h
  ```
  Matches Hawkins & Olwell two-sided CUSUM exactly.
- K and H sourced from `CUSUM_K_SIGMA=0.5`, `CUSUM_H_SIGMA=5.0` — correct.

**SCADA-2 (NOTE):** No `cusum.reset(sensor)` call after drift alarm fires (L454-457). Once S+ or S- exceeds H, subsequent updates keep accumulating. The CUSUM stays in alarm state until residuals naturally bring the accumulator back below H. This is a valid "non-resetting CUSUM" variant, but means drift alarms cascade to all subsequent rows. The `CUSUMDriftDetector` class provides `reset()` but it's never called in the bootstrap detector. Consider whether post-alarm reset matches the intended operational semantics.

**Status: PASS WITH NOTES**

### 6.4 Precision Degradation (Classifier 4)
**File:** pass2_bootstrap_detect.py L459-475
**Spec:** high-pass Butterworth + rolling std > noise_target * NOISE_ALPHA

- Window: `raw_values[max(0, row_idx - SEQ_LEN + 1) : row_idx + 1, s_idx]` — 1D per-sensor.
- Delegates to `NoiseDetector.detect(window, sensor)` (noise_detector.py L38-59):
  - 2nd-order Butterworth high-pass filter (`butter(N=2, Wn=0.1, btype="high")`) — correct.
  - `filtfilt(b, a, series)` — zero-phase filter, correct.
  - `pd.Series(filtered).std()` compared against `target * NOISE_ALPHA` — correct threshold logic.
  - Min 10 samples guard (L50) — correct for `filtfilt` padlen requirements.
- Thresholds from config: `NOISE_ALPHA=3.0`, `SENSOR_NOISE_TARGETS` per-sensor — correct.

**Status: PASS**

### 6.5 Dropout (Classifier 5)
**File:** pass2_bootstrap_detect.py L477-502
**Spec:** P consecutive null steps

- Window: `raw_values[max(0, row_idx - DROPOUT_CONSECUTIVE_P + 1) : row_idx + 1]` — P-step window.
- Converts to list of dicts: `{sensor: None if NaN else float(val)}` — correct NaN → None mapping.
- Delegates to `DropoutDetector.detect(rows)` (dropout_detector.py L32-52):
  - Counts consecutive steps where `_any_null(row)` is True (any sensor null/NaN).
  - Alarms when `consecutive >= CONSECUTIVE_THRESHOLD`.
  - `CONSECUTIVE_THRESHOLD` sourced from `DROPOUT_CONSECUTIVE_P=5` — correct.

**Status: PASS**

---

## 7. CORRECTNESS FINDINGS

### HIGH: Inference runs on raw CSV instead of resampled data

**ID:** COR-1
**File:** `src/api/routes/bootstrap.py` L126-128
**Severity:** HIGH

```python
full_df: pd.DataFrame = pd.read_csv(
    tmp_path, index_col=0, parse_dates=True
)
```

The Pass 2 model is trained on data that went through Pass 1's full pipeline: CSV parsing via CSVAdapter, timestamp detection, 5-minute resampling (`BOOTSTRAP_RESAMPLE_INTERVAL`), linear interpolation of climate columns, and gas column masking. The model learns temporal patterns at 5-minute cadence.

However, for inference, the route re-reads the **raw uploaded CSV** with `pd.read_csv(tmp_path, index_col=0, parse_dates=True)`. This raw data:
1. Has not been resampled to 5-minute intervals (irregular timestamps).
2. Has not been interpolated (gaps in climate columns).
3. May have `index_col=0` selecting a non-timestamp column as the index.
4. Has the same column names as the raw CSV, which may or may not match `_CLIMATE_COLS` (depends on whether the raw CSV already uses standardized names like "temperature" or has vendor-specific names like "Greenhouse Temperature [°C]").

The inference method `Pass2BootstrapDetector.infer()` looks for `_CLIMATE_COLS` = `["temperature", "humidity", "co2", "light"]` in `full_df.columns`. If the raw CSV uses vendor column names, no columns match and inference produces trivially non-anomalous results.

**Recommended fix:** Use the resampled DataFrame from Pass 1 for inference. The `BootstrapPass1Result` already holds the full resampled DataFrame in `pass1_result.clean_df` and `pass1_result.anomaly_df`. Reconstruct the full DataFrame by concatenating and sorting:
```python
full_df = pd.concat([pass1_result.clean_df, pass1_result.anomaly_df]).sort_index()
```

### MEDIUM-HIGH: `tenant_id` accepted but unused

**ID:** COR-2
**File:** `src/api/routes/bootstrap.py` L55
**Severity:** MEDIUM-HIGH

```python
tenant_id: str = Form(...),
```

The `tenant_id` form parameter is required by the endpoint but never used anywhere in the pipeline. This means:
- All tenants write to the same `BOOTSTRAP_CLEAN_PATH` / `BOOTSTRAP_ANOMALY_PATH` files (config.py L639-640).
- Concurrent requests from different tenants would overwrite each other's clean/anomaly CSVs.
- No multi-tenant isolation exists despite the parameter suggesting it.

This is both a correctness issue (data race) and an architectural concern (multi-tenant isolation contract not fulfilled).

### MEDIUM-HIGH: No test coverage for bootstrap pipeline

**ID:** COR-3
**Files:** All reviewed files
**Severity:** MEDIUM-HIGH

No test files exist for any component of the two-pass bootstrap pipeline:
- No `tests/*bootstrap*`
- No `tests/*psychrometric*`
- No `tests/*cold_start*`
- No `tests/*pass2*` (for the bootstrap variant)

The `AGENT_MEMORY.md` notes that Pass 1 had "16 functional tests pass (9 psychrometric + 7 pipeline)" and the API wiring had smoke imports, but these test files are not present in the `tests/` directory. They may have been run inline during agent execution but never persisted.

This violates the universal guardrail "Test coverage below 80% on critical paths" — the bootstrap pipeline is a critical path (onboarding new tenants) with 0% test coverage.

---

## 8. PERFORMANCE FINDINGS

### MEDIUM: Row-by-row psychrometric validation

**ID:** PERF-1
**File:** `engines/cold_start_bootstrap.py` L239-260
**Severity:** MEDIUM

```python
for _, row in df.iterrows():
    ...
    result = validate_row(t_air=float(t_val), rh=float(rh_val), hd_measured=float(hd_val))
    flags.append(result == "FAILED_PSYCHROMETRIC_LIMIT")
```

`df.iterrows()` is extremely slow on large DataFrames (1000x slower than vectorized operations). For SCADA datasets with 100K+ rows (common for multi-month greenhouse data at 5-min cadence), this could take minutes.

The Tetens equation and HD calculation are vectorizable with NumPy:
```python
e_s = 0.61078 * np.exp(17.27 * t_air / (t_air + 237.3))
e_a = e_s * (rh / 100.0)
hd_calc = 216.7 * (e_s - e_a) / (t_air + 273.15)
flags = np.abs(hd_measured - hd_calc) > PSYCHROMETRIC_HD_TOLERANCE
```

The existing row-by-row approach was a deliberate choice to handle per-row `ValueError` from unphysical inputs gracefully (noted in `AGENT_MEMORY.md`). A vectorized version could pre-filter unphysical rows, then vectorize the rest.

---

## 9. CODE QUALITY FINDINGS

### LOW: Duplicated timestamp helper

**ID:** CQ-1
**Files:** `pass2_bootstrap_detect.py` L517-523, `bootstrap.py` L39-49
**Severity:** LOW

`_timestamp_to_iso` (engine) and `_ts_to_iso` (route) implement identical logic: tz-naive Timestamps get `tz_localize("UTC")` then `.isoformat()`. Should be extracted to a shared utility to avoid drift.

### LOW: Inconsistent `Optional` syntax

**ID:** CQ-2
**File:** `pass2_bootstrap_detect.py` L17
**Severity:** LOW

Uses `from typing import Optional` and `Optional[LSTMVAE]` (L82) while the rest of the codebase uses `X | None` syntax (e.g., `str | None` in `cold_start_bootstrap.py` L66). Minor consistency issue.

### NOTE: Engine dataclass mutability

**ID:** CQ-3
**File:** `pass2_bootstrap_detect.py` L57-67
**Severity:** NOTE

`BootstrapRowResult` uses `@dataclass` (mutable) while `BootstrapPass1Result` uses `@dataclass(frozen=True)`. The mutability of `BootstrapRowResult` is intentionally exploited by the API route to overlay `pass1_filtered` and `physics_validation_status` (bootstrap.py L142, L155). This is a valid design choice but should be documented.

### NOTE: Missing upload size limit

**ID:** CQ-4
**File:** `src/api/routes/bootstrap.py` L78-80
**Severity:** NOTE

No maximum file size validation on the uploaded CSV. A very large file (multi-GB) would be written entirely to disk and processed, potentially exhausting disk space or memory. Consider adding a size check during the chunked read loop.

### NOTE: Fragile error message matching

**ID:** CQ-5
**File:** `src/api/routes/bootstrap.py` L102
**Severity:** NOTE

```python
if msg.startswith("Insufficient clean rows"):
```

The 400 vs 500 status code decision depends on string-prefix matching against the `ValueError` message from `cold_start_bootstrap.py`. If the message wording changes, the route silently falls through to the 500 handler. Consider using a custom exception class (e.g., `InsufficientDataError`) instead.

---

## 10. SUMMARY

### Verdict: **NEEDS FIXES**

| Category | Status | Findings |
|----------|--------|----------|
| Psychrometric math | PASS | Exact spec match |
| Hardcoded numbers | NEEDS FIXES | 4 values not in config (HC-1 through HC-4) |
| Security | PASS WITH NOTES | 2 exception message leaks (SEC-1, SEC-2) |
| Error handling | PASS | All error codes correct |
| Guardrail compliance | PASS | All guardrails satisfied |
| SCADA spec alignment | PASS WITH NOTES | 2 notes (SCADA-1: threshold unit comment, SCADA-2: no CUSUM reset) |
| Correctness | NEEDS FIXES | 1 HIGH (COR-1), 2 MEDIUM-HIGH (COR-2, COR-3) |
| Performance | MEDIUM | 1 vectorization opportunity (PERF-1) |
| Code quality | LOW | 5 notes (CQ-1 through CQ-5) |

### Blocking issues (must fix before merge):

1. **COR-1 (HIGH):** Inference on raw CSV instead of Pass 1 resampled data — model/data mismatch.
2. **COR-3 (MEDIUM-HIGH):** Zero test coverage for the entire bootstrap pipeline.

### Should fix:

3. **COR-2 (MEDIUM-HIGH):** `tenant_id` unused — concurrent requests create data races on shared output files.
4. **HC-1, HC-2 (MEDIUM):** Hardcoded `100` and `10` thresholds should be in config.
5. **SEC-1, SEC-2 (MEDIUM):** Exception messages leak internal details to clients.
6. **CQ-5 (NOTE):** Fragile string-based error classification — use custom exception.

### Non-blocking observations:

7. **SCADA-2:** CUSUM never reset after alarm — cascading drift tags.
8. **PERF-1:** `iterrows()` psychrometric validation — vectorizable for large datasets.
9. **HC-3, HC-4:** Minor hardcoded values (NaT ratio, random state).
10. **CQ-1 through CQ-4:** Code quality and consistency notes.

---

*Review performed read-only. No files modified.*

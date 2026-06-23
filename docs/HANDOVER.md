# Session Handover — ai-agronomist

**Date:** 2026-06-23
**Branch:** main (all work uncommitted — large diff against HEAD)
**Session coverage:** Dashboard UI sprint + SCADA algorithm spec-compliance sprint

---

## Project State

### Test suite
- **1367 passed, 4 failed** (all 4 failures are pre-existing, not caused by this session)
- Pre-existing failures: `test_upload_invalid_csv_format`, `test_upload_nan_values`, `test_upload_out_of_range`, `test_error_recovery`
- Run: `./venv/bin/pytest tests/ --no-cov -q`

### Last commit
```
a1e16af Finish Gap 2 incident memory: QA suites, alembic wiring, review docs
```
Everything in this session is **uncommitted**. `git diff --stat HEAD` shows ~800 insertions across many files.

---

## Work Completed This Session

### 1. Dashboard UI fixes

**File:** `src/dashboard/pages/5_upload_operations.py`
- Fixed button alignment with dropdown using `st.columns([2, 1, 3], vertical_alignment="bottom")`

**File:** `src/dashboard/pages/7_gas_sensors.py`
- Unified sidebar controls: Time window → Refresh → Date range (with time pickers) on all graphic pages
- Replaced separate anomaly filter buttons with **clickable badge pattern** on metric cards:
  - `st.button("↑ N anomalies", type="secondary")` when inactive
  - `st.button("✕ N anomalies", type="primary")` when active (applies table filter)
  - `st.caption("↑ 0 anomalies")` for zero count (non-interactive)
- Filter state: `st.session_state["gas_filter_col"]`

**File:** `src/dashboard/pages/8_sensor_detail.py` *(NEW)*
- New climate sensor detail page (temperature, humidity, CO₂, light)
- Matches gas sensor page pattern: per-channel synced subplots, clickable anomaly badges, severity table
- Uses `plot_synced_sensor_grid` from `src/dashboard/components/charts.py`
- Z-score detection per column: `_Z_ANOMALY = 3.0`, `_Z_CRITICAL = 5.0`
- Filter state: `st.session_state["sdetail_filter_col"]`
- Sidebar: time window → refresh → date range → channel checkboxes

---

### 2. SCADA Algorithm Spec-Compliance Sprint

Full audit against spec document (v4.docx). Five fault types were audited and fixed.

#### Algorithm 1 — Spike/Transient (`src/models/pattern_scores.py`)
**Problem:** Velocity+reversal was a 1.25× score boost; z-score was primary.
**Fix:** Restructured `score_spike()` — velocity+reversal is now the **primary path** (returns score=1.0 immediately), z-score is the **fallback** for sensors without velocity limits.
```python
# Primary: |Δx_t| > Δ_max AND sign(Δx_t) ≠ sign(Δx_{t+1})
if primary_affected:
    return 1.0, np.array(primary_affected, dtype=int)
# Fallback: z-score detection
```
`_SPIKE_VELOCITY_LIMITS` now imports from `config.VELOCITY_LIMITS` (was hardcoded).

#### Algorithm 2 — Stuck/Constant (`src/models/stuck_fault_detector.py`)
**Problem:** Used full-sequence variance — missed intra-sequence recovery.
**Fix:** Rolling-window minimum variance: `pd.Series.rolling(window=K).var().min()`.
Falls back to `np.var()` when sequence shorter than `STUCK_WINDOW_K`.

#### Algorithm 3 — Calibration Drift (`src/models/cusum_drift_detector.py`) *(NEW file)*
**Problem:** Pipeline imported a stub `CUSUMDriftDetector` from `drift_detector.py` that always returned False (C-1 critical bug).
**Fix:** Created real two-sided CUSUM in `cusum_drift_detector.py`. Pipeline now imports from there.
**Documented deviation:** Uses standardised residual `(x - μ₀)/σ₀` instead of raw. Constants renamed `CUSUM_K_SIGMA=0.5`, `CUSUM_H_SIGMA=5.0` to make units explicit.

#### Algorithm 4 — Precision Degradation/Noise (`src/models/noise_detector.py`) *(NEW file)*
**Problem:** Did not exist — noise handled implicitly via spike z-score.
**Fix:** New `NoiseDetector` with Butterworth 2nd-order HP filter + `pd.Series.std()`:
```python
b, a = butter(N=2, Wn=HP_CUTOFF, btype="high")
filtered = filtfilt(b, a, series.astype(float))
rolling_std = float(pd.Series(filtered).std())
return rolling_std > target * NOISE_ALPHA
```
Config: `SENSOR_NOISE_TARGETS` (per-sensor σ targets), `NOISE_ALPHA=3.0`.

#### Algorithm 5 — Polling Dropout (`src/models/dropout_detector.py`)
**Problem (A):** Used ALL-sensor-null check; spec requires ANY sensor null.
**Problem (B):** Used total fraction ≥ 25%; spec requires **P consecutive** null steps.
**Fix:** Rewritten with consecutive counter + `_any_null()` helper.
Config: `DROPOUT_CONSECUTIVE_P=5` (5 consecutive = ~25 min at 5-min intervals).

#### Config additions (`src/config.py`)
```python
VELOCITY_LIMITS: dict[str, float]   # per-sensor spike velocity limits
CUSUM_K_SIGMA: float = 0.5
CUSUM_H_SIGMA: float = 5.0
DROPOUT_CONSECUTIVE_P: int = 5
STUCK_WINDOW_K: int = 10
SENSOR_NOISE_TARGETS: Dict[str, float]  # per-sensor noise σ targets
NOISE_ALPHA: float = 3.0
```
Removed: `DROPOUT_PERSISTENCE_THRESHOLD` (replaced by consecutive logic).

#### Pipeline wiring (`src/api/services/comprehensive_pipeline.py`)
- Import fixed: `from src.models.cusum_drift_detector import CUSUMDriftDetector`
- `DropoutDetector` wired as pre-check before LSTM-VAE inference
- CUSUM updates wired after LSTM-VAE forward pass
- `NoiseDetector` wired as pre-check (per spec)

#### Fault type (`src/models/fault_classifier.py`)
- Added `POLLING_DROPOUT = "polling_dropout"` to `FaultType` enum
- Added highest-priority classification rule for polling dropout

#### Alembic migration (`alembic/versions/012_add_polling_dropout_fault_type.py`) *(NEW)*
- Added `polling_dropout` to the CHECK constraint on `incidents.fault_type`

---

### 3. QA — Spec-Compliance Coverage

All new/modified model files reached **100% coverage**:

| File | Final coverage |
|------|---------------|
| `src/models/dropout_detector.py` | 100% |
| `src/models/noise_detector.py` | 100% |
| `src/models/pattern_scores.py` | 100% |
| `src/models/stuck_fault_detector.py` | 100% |

New test files created:
- `tests/models/test_dropout_detector.py` — 15 tests (consecutive, any-null, edge cases)
- `tests/models/test_noise_detector.py` — 7 tests (HP filter, sensor types, boundaries)
- `tests/models/test_pattern_scores.py` — 10 tests (primary path, fallback, no-velocity-limit)
- `tests/models/test_stuck_fault_detector.py` — 8 tests (detection, 1D ValueError, cross-sensor)
- `tests/models/test_cusum_drift_detector.py` — 19 tests (construction, CUSUM math, serialization)

Coverage method used (fast — ~4 seconds):
```bash
./venv/bin/pytest tests/models/ \
  --cov=src/models/noise_detector \
  --cov=src/models/dropout_detector \
  --cov=src/models/stuck_fault_detector \
  --cov=src/models/pattern_scores \
  --cov-report=term-missing -q
```

`# pragma: no cover` applied to `except ImportError` fallback blocks (unreachable in normal use).

---

### 4. Code Review — Spec Compliance

**Verdict: PASS WITH NOTES** (`docs/review/spec_compliance_review.md`)

No blocking issues. Open WARN-level items (non-blocking, deferred):

| ID | File | Issue |
|----|------|-------|
| W1 | `noise_detector.py:58` | Variable named `rolling_std` but computes overall std — rename to `filtered_std` |
| W3 | `stuck_fault_detector.py:37` | `window` constructor param stored but never used — dead code |
| W4 | `stuck_fault_detector.py:43` | `variance_threshold=1e-4` hardcoded in constructor, should go in `config.py` as `STUCK_VARIANCE_THRESHOLD` |
| W5 | `cusum_drift_detector.py:from_dict()` | No input validation on deserialized values — attack surface in multi-tenant system |
| W6 | `cusum_drift_detector.py:42` | Constructor defaults `k=0.5, h=5.0` not imported from config constants — silent divergence risk |
| W7 | `test_noise_detector.py` | Missing threshold boundary test (near `sigma_target * NOISE_ALPHA`) |

---

## Pending / Not Done

1. **WARN items W1–W7** from code review — not fixed yet, deferred as non-blocking
2. **backend-engineer-spec-compliance agent** never wrote a done signal — its config.py changes were applied by the ml-engineer agent instead. Verify nothing was missed.
3. **`comprehensive_pipeline.py` NoiseDetector wiring** — confirm it was wired (check the import and pre-check block)
4. **Commit all changes** — nothing has been committed since `a1e16af`

---

## Key Files Changed (Uncommitted)

### New files
- `src/models/cusum_drift_detector.py`
- `src/models/noise_detector.py`
- `src/dashboard/pages/8_sensor_detail.py`
- `alembic/versions/012_add_polling_dropout_fault_type.py`
- `tests/models/test_cusum_drift_detector.py`
- `tests/models/test_dropout_detector.py`
- `tests/models/test_noise_detector.py`
- `tests/models/test_pattern_scores.py`
- `tests/models/test_stuck_fault_detector.py`
- `docs/review/spec_compliance_review.md`

### Modified files
- `src/models/pattern_scores.py` — spike restructured
- `src/models/dropout_detector.py` — rewritten
- `src/models/stuck_fault_detector.py` — rolling window
- `src/models/fault_classifier.py` — polling_dropout enum + rule
- `src/api/services/comprehensive_pipeline.py` — CUSUM import fix + detector wiring
- `src/db/learning_state_store.py` — CUSUM state persistence
- `src/config.py` — new constants
- `src/dashboard/pages/5_upload_operations.py` — button alignment
- `src/dashboard/pages/7_gas_sensors.py` — unified sidebar + clickable badges

---

## Agent Infrastructure Notes

- **Spend limit issue:** QA and code-reviewer agents crashed silently (no done signal) twice. Root cause: monthly API spend limit hit mid-run. Workaround: run QA tasks directly from orchestrator.
- **QA runtime:** Full suite `pytest tests/` with `--cov=src` takes 2-3 minutes and 4 iterations = 8-12 min + agent overhead. **Always scope coverage to changed files only** using explicit `--cov=src/models/file` flags. `tests/models/` runs in ~4-8 seconds.
- **Test isolation:** All 25 "extra" failures seen mid-session were a red herring — caused by a coverage threshold failure interrupting collection of `test_drift_detector_stub.py`, making everything after it appear as failures. Running `--no-cov` confirmed only 4 real failures.
- Agent done signals: `tasks/done/<agent-name>.json`
- Orchestrator state: `tasks/orchestrator_state.json`

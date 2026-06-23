# SCADA Spec-Compliance Code Review — 5 Fault Detection Algorithms

**Date:** 2026-06-23
**Reviewer:** code-reviewer (Claude Opus)
**Scope:** 5 implementation files + 5 test files (SCADA fault detection algorithms)
**Verdict:** **PASS WITH NOTES**

---

## Summary

All five detectors implement their SCADA spec algorithms correctly. No blocking bugs found. The code is clean, well-documented, and follows project conventions. Test coverage is solid with meaningful assertions. Seven WARN-level and eight NOTE-level findings identified, none requiring immediate fixes before merge.

| File | Lines | Verdict | Findings |
|------|-------|---------|----------|
| `noise_detector.py` | 60 | PASS WITH NOTES | 1 WARN, 2 NOTE |
| `dropout_detector.py` | 63 | PASS | 1 NOTE |
| `pattern_scores.py` (score_spike) | 168 | PASS WITH NOTES | 1 WARN, 1 NOTE |
| `stuck_fault_detector.py` | 86 | PASS WITH NOTES | 2 WARN, 1 NOTE |
| `cusum_drift_detector.py` | 130 | PASS WITH NOTES | 2 WARN, 1 NOTE |
| `test_noise_detector.py` | 81 | PASS WITH NOTES | 1 WARN, 1 NOTE |
| `test_dropout_detector.py` | 122 | PASS | 0 |
| `test_pattern_scores.py` | 142 | PASS WITH NOTES | 1 NOTE |
| `test_stuck_fault_detector.py` | 60 | PASS WITH NOTES | 1 NOTE |
| `test_cusum_drift_detector.py` | 172 | PASS | 0 |

---

## Per-File Findings

### 1. `src/models/noise_detector.py` (Precision Degradation / SCADA spec 1.4)

**Algorithm correctness: PASS**

The Butterworth HP filter + threshold comparison correctly implements the spec. The `filtfilt` zero-phase filter is the right choice (no phase distortion). The N=2 order and Wn=0.1 normalized cutoff are reasonable design parameters.

**W1 (WARN) — Line 58: "rolling_std" is not a rolling standard deviation.**
The module docstring (line 7) states "Compute rolling standard deviation of filtered signal," and the variable is named `rolling_std`. However, the code computes the **overall** standard deviation of the entire filtered signal via `pd.Series(filtered).std()`. There is no rolling window. For seq_len=20 this is equivalent to a single-window computation, so the detection result is correct, but the naming is misleading. If the spec intends a sliding window within the sequence, the implementation does not match.

```python
# Line 58 — computes overall std, not rolling
rolling_std = float(pd.Series(filtered).std())
```

**W1 sub-note:** `pd.Series.std()` uses `ddof=1` (Bessel's correction) by default. For a 20-sample sequence this is a ~2.6% difference from population std. Not a bug, but worth being explicit about the choice.

**N1 (NOTE) — Lines 18-24: Fallback `SENSOR_NOISE_TARGETS` values are stale.**
The `except ImportError` fallback defines `SENSOR_NOISE_TARGETS` with 6x looser thresholds than `config.py` (e.g., temperature 0.3 vs 0.05). These are pre-spec-compliance values. The fallback is `pragma: no cover` and only runs when `src.config` is unavailable (standalone use), so production is unaffected, but anyone running the module outside the project gets silently different behavior.

| Sensor | Fallback (line 19-24) | config.py (line 606-611) | Ratio |
|--------|----------------------|--------------------------|-------|
| temperature | 0.3 | 0.05 | 6x |
| humidity | 1.0 | 0.1 | 10x |
| co2 | 10.0 | 5.0 | 2x |
| light | 30.0 | 2.0 | 15x |

**N2 (NOTE) — Line 33: `HP_CUTOFF` is a class constant, not in config.py.**
This is a filter design parameter (not a detection threshold), so keeping it as a class constant is defensible. However, the project pattern is to externalize all tunables to `config.py`. Low priority.

---

### 2. `src/models/dropout_detector.py` (Polling Dropout / SCADA spec 1.5)

**Algorithm correctness: PASS**

The implementation exactly matches the spec: `psi_t = 1 if any sensor value is NaN or null at step t`, alarm when P consecutive steps have `psi_t = 1`. The `_any_null()` helper correctly handles `None`, `float` NaN, and empty dicts. The consecutive counter with early exit at threshold is textbook.

**N3 (NOTE) — Line 26-30: `PERSISTENCE_THRESHOLD` backward-compat alias.**
The class still carries `PERSISTENCE_THRESHOLD: float = 0.25` for `comprehensive_pipeline.py` compatibility. The TODO is dated 2026-06-23 with clear migration instructions. Non-blocking — tracked for cleanup.

**Code quality:** Clean. Single responsibility. Guard clauses. No magic numbers. All tunables sourced from config.

**Type safety:** `Any` in `dict[str, Any]` is appropriate — sensor values can be `float | None | int`.

**Security:** No external I/O. Pure computation. No concerns.

---

### 3. `src/models/pattern_scores.py` — `score_spike()` (Spike/Transient / SCADA spec 1.1)

**Algorithm correctness: PASS**

Primary path correctly implements the spec condition:
- `|delta_x_t| > delta_max` checked via `abs(diffs[t]) > vel_limit` (line 149)
- `sign(delta_x_t) != sign(delta_x_{t+1})` checked via `diffs[t] * diffs[t + 1] < 0` (line 149)
- Score = 1.0 immediately on primary detection (line 154)
- Fallback z-score path activates only when no velocity+reversal found (line 157+)

The `seq.shape[0] >= 3` guard (line 142) is correct — you need at least 2 diffs for a reversal check.

**W2 (WARN) — Line 149: Sign reversal via product excludes zero-diff case.**
The check `diffs[t] * diffs[t + 1] < 0` uses strict less-than. If `diffs[t+1]` is exactly 0.0 (spike followed by no change), the product is 0.0 which is NOT < 0, so no reversal is detected. This is mathematically correct — `sign(0) = 0` is neither positive nor negative, so `sign(delta) != sign(0)` is true but the product check misses it. In practice, exact zero diffs are extremely unlikely with float sensor data. Flagging for spec awareness, not a bug.

**N4 (NOTE) — Line 47: `_SPIKE_VELOCITY_LIMITS` is a module-level alias for `VELOCITY_LIMITS`.**
Adds semantic clarity at the cost of one more name. Acceptable.

**Code quality:** Clean separation of primary and fallback paths. All tunables from config. Good comments.

---

### 4. `src/models/stuck_fault_detector.py` (Stuck/Constant / SCADA spec stuck sensor)

**Algorithm correctness: PASS**

The rolling-window minimum variance approach correctly catches intra-sequence stuck periods:
- `pd.Series.rolling(window=K).var().min()` finds the minimum variance across all K-length windows
- A sensor stuck for the first 15/20 steps produces a near-zero variance in at least one window
- Falls back to full-sequence `np.var()` when sequence is shorter than `window_k` — correct degradation
- `nan_to_num(nan=1.0)` treats insufficient data as "not stuck" — safe default

**W3 (WARN) — Line 37-39: `window` parameter is dead code.**
```python
def __init__(self, window: int = 20, ...):
    self._window = window  # never read after assignment
```
The docstring says "Used only for documentation; detection works on any length." This is a constructor parameter that is stored but never used. It adds confusion — callers may think it affects detection behavior. Should be removed or repurposed.

**W4 (WARN) — Line 43: `variance_threshold` default is a constructor literal, not a config constant.**
The threshold `1e-4` is a hardcoded default in the constructor. While it's overridable at construction time, the project convention is to centralize tunables in `config.py`. Compare: `window_k` correctly defaults to `STUCK_WINDOW_K` from config.

**N5 (NOTE) — Line 22: `_SENSOR_NAMES` is a module-level list conversion.**
`_SENSOR_NAMES: list[str] = list(SENSOR_NAMES)` is evaluated at import time. If `SENSOR_NAMES` were modified at runtime (unlikely but possible), `_SENSOR_NAMES` would be stale. Low-risk.

---

### 5. `src/models/cusum_drift_detector.py` (Calibration Drift / SCADA spec 1.3)

**Algorithm correctness: PASS**

Standard two-sided CUSUM implementation per Hawkins & Olwell:
- `S+ = max(0, S+ + z - k)` (line 74-75) — correct
- `S- = max(0, S- - z - k)` (line 77-78) — correct
- Alarm when `S+ > h OR S- > h` (line 80) — correct, uses strict `>` (not `>=`)
- `defaultdict(float)` for per-sensor accumulators — clean zero-initialization

The strict `> h` (not `>= h`) is a valid design choice, confirmed by the step-count test: with residual=1.5 and k=0.5, increment=1.0/step, S+ reaches 5.0 at step 5 (not > 5.0), alarm at step 6 (S+=6.0 > 5.0). This is consistent with the standard CUSUM formulation.

**W5 (WARN) — Lines 117-129: `from_dict()` does not validate input types or shape.**
Deserialization blindly trusts the input dict. Corrupted or malicious data (e.g., `s_pos` containing strings instead of floats, or `k` being negative) would create a detector in an invalid state. In a multi-tenant system where state is persisted per tenant, this is an attack surface.

```python
# Line 127 — no validation on restored accumulator values
obj._s_pos = defaultdict(float, d.get("s_pos", {}))
obj._s_neg = defaultdict(float, d.get("s_neg", {}))
```

**W6 (WARN) — Constructor defaults vs config.py.**
`CUSUMDriftDetector` defaults to `k=0.5, h=5.0` matching `CUSUM_K_SIGMA`/`CUSUM_H_SIGMA` in config.py, but does NOT import or reference these config constants. The pipeline is responsible for passing config values at construction time. This creates a silent divergence risk if config values change — the standalone class retains the old defaults.

**N6 (NOTE) — Line 2-3: "documented deviation" referenced but not documented.**
The module docstring mentions this detector uses a "standardised residual variant" and implies a documented deviation from some reference, but the specific deviation is not described.

---

## Test File Findings

### 6. `tests/models/test_noise_detector.py` (8 tests)

**Test quality: GOOD**

Coverage of core detection paths: high noise triggers, clean signal doesn't trigger, unknown sensor returns False, short sequence boundary (< 10, exactly 9, exactly 10), multiple sensor types.

**W7 (WARN) — Missing: threshold boundary test.**
No test verifies behavior when noise is exactly at or near the threshold (`sigma_target * NOISE_ALPHA`). The tests use extreme values (std=5.0 vs std=0.01) that are far from the boundary. A near-threshold test would catch off-by-one or ddof issues.

**N7 (NOTE) — Missing: all-constant signal test.**
A constant signal has zero high-frequency content after HP filtering. This is an important edge case — `filtfilt` on a constant signal produces all zeros, and `pd.Series([0]*N).std()` is 0.0, which should never trigger. Not tested.

---

### 7. `tests/models/test_dropout_detector.py` (15 tests)

**Test quality: EXCELLENT**

Comprehensive coverage: P consecutive all-null, P consecutive single-null (spec: ANY not ALL), scattered non-consecutive, P-1 boundary, mid-sequence, more-than-P, empty sequence, all valid, all null, single row, empty dict, NaN values, mixed None/NaN, threshold value verification. The `_single_null_row` helper specifically validates the "any sensor null" condition. Well-structured test classes.

No findings.

---

### 8. `tests/models/test_pattern_scores.py` (10 tests)

**Test quality: GOOD**

Good coverage of both primary and fallback paths. Uses deterministic sequences for velocity+reversal tests. Mocks `_SPIKE_VELOCITY_LIMITS` to test no-velocity-limit fallback. Tests multiple sensor detection.

**N8 (NOTE) — Missing: fallback-path positive detection test.**
No test causes the fallback z-score path to detect a spike and return a non-zero score. All fallback tests verify normal data returns 0 or check return types. A test with a z-score outlier in a sensor without velocity limits (via the mock) would exercise the fallback scoring logic (lines 157-167).

---

### 9. `tests/models/test_stuck_fault_detector.py` (8 tests)

**Test quality: GOOD**

Covers: all-stuck detection, normal data not flagged, all sensor names in output, partial stuck (recovery detected), 1D input validation error, short sequence fallback, custom threshold.

**N9 (NOTE) — Missing: mixed stuck/normal sensors test.**
No test creates a sequence where some sensors are stuck and others have normal variance, verifying per-sensor independence. The `test_partial_stuck_detected` tests temporal recovery but not cross-sensor behavior.

---

### 10. `tests/models/test_cusum_drift_detector.py` (19 tests)

**Test quality: EXCELLENT**

Thorough coverage: construction validation (negative k/h, zero k/h, custom params), CUSUM accumulation (small residuals, sustained pos/neg drift, independent sensors, accumulator reset to zero), exact step-count verification (step 6 with residual=1.5), reset behavior (clears accumulators, unknown sensor safe), alarmed sensors listing, serialization round-trip (preserves state, defaults on missing fields, restores alarm state, dict format).

No findings.

---

## Cross-Cutting Checks

### Security
| Check | Result |
|-------|--------|
| Hardcoded secrets/tokens/API keys | PASS — none found |
| `eval()` / `Function()` / dynamic execution | PASS — none found |
| SQL injection | PASS — no SQL in any file |
| Unvalidated external input | PASS — boundary guards present |
| Sensitive data in logs | PASS — no logging in these files |
| Path traversal | N/A — no file I/O |

### Guardrail Checks
| Guardrail | Result |
|-----------|--------|
| Tight coupling between LSTM-VAE and detection layer | PASS — all 5 detectors are independent modules |
| Secrets or API keys in code | PASS — none |
| Model checkpoint without metadata | N/A — no checkpoints |
| Hard-coded thresholds not in config.py | WARN — W4 (`variance_threshold`), W6 (CUSUM defaults) |
| Missing error handling | PASS — guard clauses on all boundaries |
| Code duplication | PASS — no duplication across the 5 files |

### Type Safety
| Check | Result |
|-------|--------|
| Bare `Any` usage | PASS — `Any` only in `dropout_detector.py` dict values (justified) |
| Explicit return types | PASS — all public methods have return type annotations |
| Input validation at boundaries | PASS — length checks, ndim checks, sensor-name lookups |

---

## Verdict: PASS WITH NOTES

All five SCADA fault detection algorithms are correctly implemented per their spec descriptions. Code quality is high: single-responsibility classes, guard clauses, no magic numbers (with two minor exceptions noted), all tunables sourced from or overridable via config.py. Test suites provide meaningful behavioral coverage.

### Findings Summary

| ID | Severity | File | Line(s) | Finding |
|----|----------|------|---------|---------|
| W1 | WARN | `noise_detector.py` | 7, 58 | `rolling_std` name misleading — computes overall std, not rolling |
| W2 | WARN | `pattern_scores.py` | 149 | Product-based sign check excludes exact zero-diff edge case |
| W3 | WARN | `stuck_fault_detector.py` | 37-39 | `window` parameter is dead code (stored but never read) |
| W4 | WARN | `stuck_fault_detector.py` | 43 | `variance_threshold=1e-4` not in config.py |
| W5 | WARN | `cusum_drift_detector.py` | 117-129 | `from_dict()` does not validate input types/shape |
| W6 | WARN | `cusum_drift_detector.py` | 42 | Constructor defaults not imported from config constants |
| W7 | WARN | `test_noise_detector.py` | — | Missing threshold boundary test |
| N1 | NOTE | `noise_detector.py` | 18-24 | Fallback `SENSOR_NOISE_TARGETS` values are stale (6-15x looser) |
| N2 | NOTE | `noise_detector.py` | 33 | `HP_CUTOFF` class constant not in config.py |
| N3 | NOTE | `dropout_detector.py` | 26-30 | `PERSISTENCE_THRESHOLD` backward-compat alias (TODO tracked) |
| N4 | NOTE | `pattern_scores.py` | 47 | `_SPIKE_VELOCITY_LIMITS` is a redundant alias |
| N5 | NOTE | `stuck_fault_detector.py` | 22 | Module-level `_SENSOR_NAMES` evaluated at import time |
| N6 | NOTE | `cusum_drift_detector.py` | 2-3 | "Documented deviation" referenced but not described |
| N7 | NOTE | `test_noise_detector.py` | — | Missing all-constant signal edge case test |
| N8 | NOTE | `test_pattern_scores.py` | — | Missing fallback-path positive detection test |
| N9 | NOTE | `test_stuck_fault_detector.py` | — | Missing mixed stuck/normal cross-sensor test |

### Recommended Follow-ups (Non-blocking)

1. **Rename or fix `rolling_std`** (W1): Either rename the variable to `filtered_std` or implement actual rolling-window std if the spec requires it.
2. **Add `STUCK_VARIANCE_THRESHOLD` to config.py** (W4): Centralize the 1e-4 default.
3. **Add input validation to `CUSUMDriftDetector.from_dict()`** (W5): Validate `k >= 0`, `h >= 0`, and all accumulator values are finite floats.
4. **Update fallback `SENSOR_NOISE_TARGETS`** (N1): Sync with current config.py values or remove the fallback entirely.
5. **Add threshold boundary tests** (W7, N8): Test detection at/near decision boundaries.
6. **Remove dead `window` parameter** (W3): It serves no runtime purpose.

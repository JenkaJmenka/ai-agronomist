# Synthetic Fixture Provenance Record

This file documents synthetic (generated) test fixtures. It is deliberately
separate from NATIVE_FIXTURES.md, which covers device-exported data only.

---

## Fixture: `synthetic_humidity_freeze_recovery.csv`

### Generator
- **Script:** `tests/fixtures/make_synthetic_humidity_freeze.py`
- **Deterministic:** Yes (numpy seed 42 for any stochastic elements)
- **Command:** `python tests/fixtures/make_synthetic_humidity_freeze.py`

### Source
- **Base data:** `native_july_clearsky.csv` rows 0-2015 (all 2016 rows)
- **Format:** Semicolon delimiter, decimal comma, DD.MM.YYYY HH:MM, 5-min cadence
- **Headers:** Copied verbatim from `native_july_clearsky.csv` (native Connext format)
- **No fabricated noise:** All values are either unmodified source data or
  documented injections below. The source segment is a healthy 7-day July window.

### Injections

#### 1. Humidity freeze (rows 288-637)
- **Value:** 76.0 (near healthy mean of 75.4)
- **Duration:** 350 rows = 29.17 h (> 24 h requirement for full window fill)
- **Release:** row 638 (first non-frozen sample)
- **HD during freeze:** set to 0 (consistent with frozen-sensor reading)
- **Design note:** The freeze value sits near the healthy mean so the ~209
  zero-variance frozen samples remaining in the 288-row rolling window at the
  deviation point roughly HALVE the rolling std vs the clean (exclusion-on)
  baseline. This is the OPPOSITE of the native fixture's 100.0 freeze, which
  inflates std.

#### 2. Post-release humidity deviations (rows 716-720)
- **Count:** 5 consecutive (>= COLD_RUN_STAT_MIN_CONSECUTIVE = 3)
- **Value:** 95.6 (< 100 — valid humidity reading)
- **Suppression window ends:** row 710 (release 638 + 72)
- **Margin:** deviations start at row 716, 6 rows past suppression window
- **z_contam (shift(1), ddof=0):** 7.55 → 5.58 across rows 716-720
  (all 5 rows >= 5.5 under contaminated baseline)
- **z_clean:** 3.92 → 2.89 across rows 716-720
  (all safely below 5.5 under clean baseline)
- **Discrimination:** exclusion ON → 0 humidity hits; exclusion OFF → >= 5

#### 3. Burst inside suppression window (rows 668-671)
- **Count:** 4 consecutive humidity deviations
- **Value:** 82.6 (separate from deviation_val, computed independently)
- **Location:** row 668 = release (638) + 30, inside [638, 710]
- **z_contam at burst rows:** 7.36 → 5.83 (all >= 5.5, would fire as hits)
- **Expected result:** SUPPRESSED by the fault-window suppression rule
- **Design note:** burst uses a separate, lower value to minimize std inflation
  in the deviation-row rolling window (burst values 668-671 fall within the
  shift(1) window at deviation rows 716-720)

#### 4. Temperature freeze for exact-column isolation (rows 1300-1499)
- **Value:** 21.9 (near pre-freeze temperature mean)
- **Duration:** 200 rows = 16.67 h (crosses 6 h gate)
- **Suppression window ends:** row 1572 (release 1500 + 72)
- **outdoor_temp deviations:** rows 1582-1589, value=60.61 (10 sigma, 8 rows)
- **Expected result:** outdoor_temp deviations must NOT be suppressed
  (exact-column isolation: only "temperature" is blocked, not "outdoor_temp")

#### 5. Psychrometric consistent pair (rows 150-153)
- **Location:** healthy pre-freeze region, within the baseline window
- **Values:** T=15.0, RH=90.0, HD=1.2824 (Tetens-correct)
- **Expected result:** both air-temperature and humidity hits cancelled
  (psychrometric pair suppression)

### z-score computation notes
The actual detector (`ColdRunStatisticalDetector.detect_rolling`) uses:
- `shifted = series.shift(1)` — the current value is excluded from its own window
- `rolling.std(ddof=0)` — population standard deviation, not sample std
- As deviation values enter the window of subsequent rows via shift(1), they
  inflate the rolling std and reduce z-scores (self-contamination effect)
- The generator (`make_synthetic_humidity_freeze.py`) simulates this exactly
  via binary search to find the minimum values that produce the required
  number of consecutive z >= 5.5 hits

### Measurement summary
| Parameter | Value |
|-----------|-------|
| deviation_val | 95.6 |
| burst_val | 82.6 |
| z_contam (row 716) | 7.5489 |
| z_contam (row 720) | 5.5788 |
| z_clean (row 716) | 3.9246 |
| z_clean (row 720) | 2.8924 |

"""Deterministic generator for synthetic_humidity_freeze_recovery.csv.

Produces a synthetic fixture in native Connext CSV format (semicolon delimiter,
decimal comma, DD.MM.YYYY HH:MM, 5-minute cadence) by copying a healthy
contiguous segment from native_july_clearsky.csv and injecting:

1. A humidity freeze (350 rows / 29.17 h, > 24 h) at a value near the healthy
   mean, so ~209 zero-variance frozen samples in the 288-row rolling window at
   the deviation point roughly HALVE the rolling std.
2. Post-release deviations (>= 3 consecutive) sized to discriminate
   exclusion-on vs exclusion-off.
3. A burst inside the suppression window (should be suppressed).
4. A temperature freeze + outdoor_temp deviations (exact-column isolation).
5. A psychrometric pair (air-temperature + humidity) at a healthy timestamp.

Source: native_july_clearsky.csv, ALL 2016 rows (rows 0-2015).
All injected values documented below and in SYNTHETIC_FIXTURES.md.

Usage:
    python tests/fixtures/make_synthetic_humidity_freeze.py

Deterministic: numpy.random.default_rng(seed=42) for any stochastic elements.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Ensure project root is on sys.path so `src` package is importable.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEED = 42
SOURCE_FILE = Path(__file__).parent / "native_july_clearsky.csv"
OUTPUT_FILE = Path(__file__).parent / "synthetic_humidity_freeze_recovery.csv"

# Source: use all 2016 rows from native_july_clearsky.csv (7 days).
SRC_START = 0
SRC_END = 2016  # exclusive; 2016 rows

# Humidity freeze parameters
# Freeze value near post-release segment mean (~75.6) to maximize the
# contamination effect (near-mean freeze halves the std).
FREEZE_VALUE = 76.0
FREEZE_START_ROW = 288   # after one full rolling window of healthy data
FREEZE_LENGTH = 350      # rows (= 29.17 h, > 24 h requirement)
FREEZE_END_ROW = FREEZE_START_ROW + FREEZE_LENGTH  # row 638 (release)

# Suppression window: release + 6h = release + 72 rows
SUPPRESS_END_ROW = FREEZE_END_ROW + 72  # row 710

# Deviation injection: just past suppression window (6 rows margin)
# At row 716, the 288-row window [429, 716] contains:
#   - 209 frozen rows (429-637)
#   - 79 real rows (638-716, >= min_periods=72)
DEVIATION_START_ROW = 716
DEVIATION_COUNT = 5  # >= COLD_RUN_STAT_MIN_CONSECUTIVE (3)

# Burst inside suppression window (should be suppressed)
BURST_START_ROW = FREEZE_END_ROW + 30  # row 668, inside [638, 710]
BURST_COUNT = 4

# Temperature freeze for exact-column isolation test
# Place at rows 1300-1500 (200 rows = 16.67 h, well past humidity events)
TEMP_FREEZE_START_ROW = 1300
TEMP_FREEZE_LENGTH = 200  # 200 * 5min = 16.67 h
TEMP_FREEZE_END_ROW = TEMP_FREEZE_START_ROW + TEMP_FREEZE_LENGTH  # row 1500
TEMP_SUPPRESS_END_ROW = TEMP_FREEZE_END_ROW + 72  # row 1572
# outdoor_temp deviations after temp suppression window
OUTDOOR_TEMP_DEV_START_ROW = TEMP_SUPPRESS_END_ROW + 10  # row 1582
OUTDOOR_TEMP_DEV_COUNT = 8

# Psychrometric pair (air temperature + humidity co-deviation in healthy region)
# Place at rows 150-153 (healthy pre-freeze region, within the baseline window)
PSYCHRO_PAIR_START_ROW = 150
PSYCHRO_PAIR_COUNT = 4

# Psychrometric INCONSISTENT pair (measured_plant_temp + humidity, negative control)
# Place at rows 170-173 (healthy pre-freeze region)
PSYCHRO_NEGATIVE_START_ROW = 170
PSYCHRO_NEGATIVE_COUNT = 4


def _parse_european_float(s: str) -> float:
    """Parse '3,14' -> 3.14."""
    return float(s.replace(",", "."))


def _format_european_float(v: float, precision: int = 2) -> str:
    """Format 3.14 -> '3,14'."""
    return f"{v:.{precision}f}".replace(".", ",")


def generate() -> None:
    """Generate the synthetic fixture."""
    rng = np.random.default_rng(SEED)

    # Read source
    with open(SOURCE_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    header = lines[0].rstrip("\n")
    # Extract source rows (1-indexed in file, header is line 0)
    source_lines = lines[1 + SRC_START : 1 + SRC_END]
    assert len(source_lines) == SRC_END - SRC_START, (
        f"Expected {SRC_END - SRC_START} source rows, got {len(source_lines)}"
    )

    # Parse into columns
    rows: list[list[str]] = []
    for line in source_lines:
        fields = line.rstrip("\n").split(";")
        rows.append(fields)

    # Column indices (from native Connext header):
    # 0: Timestamp
    # 1: Outdoor Temp
    # 2: Measured Humidity Deficit
    # 3: Measured Relative Humidity
    # 4: Measured Plant Temp
    # 5: Measured Greenhouse Temp (-> "temperature" after canonicalization)
    # 6: Calculated Heating Temp
    # 7: Measured Pipe 2 Temp
    # 8: Measured Screen 1
    # 9: Light Intensity
    # 10: Rain
    # 11: Measured Leeward Vent 2
    # 12: Measured Pipe 1 Temp
    # 13: Wind Speed
    COL_HUMIDITY = 3
    COL_HUMIDITY_DEFICIT = 2
    COL_GREENHOUSE_TEMP = 5  # -> "temperature"
    COL_OUTDOOR_TEMP = 1     # -> "outdoor_temp"
    COL_PLANT_TEMP = 4       # -> "measured_plant_temp"

    # ----- Measure baseline stats -----
    humidity_values = np.array([_parse_european_float(r[COL_HUMIDITY]) for r in rows])
    temp_values = np.array([_parse_european_float(r[COL_GREENHOUSE_TEMP]) for r in rows])
    outdoor_temp_values = np.array([_parse_european_float(r[COL_OUTDOOR_TEMP]) for r in rows])

    print(f"Source: native_july_clearsky.csv rows {SRC_START}-{SRC_END}")
    print(f"Humidity (full): mean={np.mean(humidity_values):.3f}, std={np.std(humidity_values):.3f}")
    print(f"Freeze value: {FREEZE_VALUE}")

    # ----- 1. Inject humidity freeze -----
    for i in range(FREEZE_START_ROW, FREEZE_END_ROW):
        rows[i][COL_HUMIDITY] = str(int(FREEZE_VALUE))
        # Zero out HD during freeze (consistent with frozen humidity)
        rows[i][COL_HUMIDITY_DEFICIT] = "0"

    # ----- 2. Compute burst AND deviation magnitudes -----
    # CRITICAL: ColdRunStatisticalDetector.detect_rolling uses:
    #   shifted = series.shift(1)        — current value excluded from window
    #   rolling.std(ddof=0)              — population std, NOT sample std
    # Earlier deviation values enter the window of later ones via shift(1),
    # inflating std and lowering z.  We must simulate this EXACTLY.
    #
    # Two-step approach:
    #   a) Compute the minimum burst value (z >= 5.5 at burst rows 668-671)
    #   b) Compute the minimum deviation value with burst already in the window
    # This keeps both values as low as possible (both must stay <= 100.0 for
    # valid humidity readings).
    import pandas as pd

    WINDOW = 288
    MIN_WINDOW_POINTS = max(5, WINDOW // 4)  # 72

    # Build the full humidity array with freeze applied (no burst/dev yet)
    full_hum = humidity_values.copy()
    for i in range(FREEZE_START_ROW, FREEZE_END_ROW):
        full_hum[i] = FREEZE_VALUE

    def _z_at_rows(arr: np.ndarray, rows: range) -> list[float]:
        """Compute rolling z (shift(1), ddof=0) at specified rows."""
        s = pd.Series(arr)
        shifted = s.shift(1)
        rmean = shifted.rolling(WINDOW, min_periods=MIN_WINDOW_POINTS).mean()
        rstd = shifted.rolling(WINDOW, min_periods=MIN_WINDOW_POINTS).std(ddof=0)
        z = (s - rmean).abs() / rstd
        return [float(z.iloc[r]) for r in rows]

    def _consecutive_hits(z_list: list[float], threshold: float = 5.5) -> int:
        c = 0
        for z in z_list:
            if z >= threshold:
                c += 1
            else:
                break
        return c

    # --- Step a: compute burst value ---
    # Burst at rows 668-671.  Window at 668 (shifted): series[380:668]
    # = 210 frozen + 30 real.  Contaminated std is very low → moderate
    # burst value suffices.
    lo_b, hi_b = 80.0, 100.0
    for _ in range(50):
        mid = (lo_b + hi_b) / 2
        test = full_hum.copy()
        for i in range(BURST_START_ROW, BURST_START_ROW + BURST_COUNT):
            test[i] = mid
        zs = _z_at_rows(test, range(BURST_START_ROW, BURST_START_ROW + BURST_COUNT))
        if _consecutive_hits(zs) >= BURST_COUNT:
            hi_b = mid
        else:
            lo_b = mid
    burst_val = round(hi_b + 0.5, 1)  # round up with margin

    # Apply burst to full_hum for deviation calculation
    full_hum_with_burst = full_hum.copy()
    for i in range(BURST_START_ROW, BURST_START_ROW + BURST_COUNT):
        full_hum_with_burst[i] = burst_val

    burst_z = _z_at_rows(full_hum_with_burst, range(BURST_START_ROW, BURST_START_ROW + BURST_COUNT))
    print(f"\nBurst value: {burst_val}")
    for idx, row in enumerate(range(BURST_START_ROW, BURST_START_ROW + BURST_COUNT)):
        print(f"  Row {row}: z_contam={burst_z[idx]:.4f}")

    # --- Step b: compute deviation value (with burst in the window) ---
    def _dev_hits(dev_val: float) -> tuple[int, list[float], list[float]]:
        """Count consecutive contam hits and measure clean z at deviation rows."""
        test = full_hum_with_burst.copy()
        for i in range(DEVIATION_START_ROW, DEVIATION_START_ROW + DEVIATION_COUNT):
            test[i] = dev_val
        zc = _z_at_rows(test, range(DEVIATION_START_ROW, DEVIATION_START_ROW + DEVIATION_COUNT))

        # Clean: NaN frozen rows
        clean = test.copy()
        for i in range(FREEZE_START_ROW, FREEZE_END_ROW):
            clean[i] = np.nan
        zcl = _z_at_rows(clean, range(DEVIATION_START_ROW, DEVIATION_START_ROW + DEVIATION_COUNT))

        return _consecutive_hits(zc), zc, zcl

    lo_d, hi_d = 85.0, 100.0
    for _ in range(50):
        mid = (lo_d + hi_d) / 2
        n, _, _ = _dev_hits(mid)
        if n >= DEVIATION_COUNT:
            hi_d = mid
        else:
            lo_d = mid
    deviation_val = round(hi_d + 0.5, 1)  # round up with margin

    # Cap at 99.9 (physically valid humidity)
    assert deviation_val <= 99.9, (
        f"deviation_val={deviation_val} exceeds valid humidity range. "
        f"Fixture design needs rework (reduce burst or freeze length)."
    )

    n_hits, z_contam_list, z_clean_list = _dev_hits(deviation_val)
    assert n_hits >= DEVIATION_COUNT, (
        f"Deviation value {deviation_val} gives only {n_hits} consecutive hits "
        f"(need {DEVIATION_COUNT}). z_contam={z_contam_list}"
    )
    assert all(z < 5.0 for z in z_clean_list), (
        f"Deviation value {deviation_val} gives z_clean >= 5.0: {z_clean_list}"
    )

    win_start = DEVIATION_START_ROW - WINDOW + 1  # 429
    frozen_in_window = FREEZE_END_ROW - win_start  # 209
    real_in_window = DEVIATION_START_ROW - FREEZE_END_ROW + 1  # 79

    print(f"\nAt deviation row {DEVIATION_START_ROW}:")
    print(f"  Window [{win_start}, {DEVIATION_START_ROW}]: {frozen_in_window} frozen + {real_in_window} real")
    print(f"  deviation_val = {deviation_val}")
    print(f"  Consecutive hits (z_contam >= 5.5): {n_hits}/{DEVIATION_COUNT}")
    for idx, row in enumerate(range(DEVIATION_START_ROW, DEVIATION_START_ROW + DEVIATION_COUNT)):
        print(f"    Row {row}: z_contam={z_contam_list[idx]:.4f}, z_clean={z_clean_list[idx]:.4f}")
    z_contam = z_contam_list[0]
    z_clean = z_clean_list[0]

    # Inject humidity deviations (set to computed value)
    for i in range(DEVIATION_START_ROW, DEVIATION_START_ROW + DEVIATION_COUNT):
        rows[i][COL_HUMIDITY] = _format_european_float(deviation_val, 2)

    # ----- 3. Burst inside suppression window -----
    for i in range(BURST_START_ROW, BURST_START_ROW + BURST_COUNT):
        rows[i][COL_HUMIDITY] = _format_european_float(burst_val, 2)

    # ----- 4. Temperature freeze (exact-column isolation) -----
    temp_pre = temp_values[TEMP_FREEZE_START_ROW - 50:TEMP_FREEZE_START_ROW]
    temp_freeze_val = round(float(np.mean(temp_pre)), 1)
    print(f"\nTemperature freeze: rows {TEMP_FREEZE_START_ROW}-{TEMP_FREEZE_END_ROW}, value={temp_freeze_val}")

    for i in range(TEMP_FREEZE_START_ROW, TEMP_FREEZE_END_ROW):
        rows[i][COL_GREENHOUSE_TEMP] = _format_european_float(temp_freeze_val, 1)

    # Outdoor_temp deviations AFTER temp suppression window
    # These must NOT be suppressed (exact-column: only "temperature" is blocked)
    ot_pre = outdoor_temp_values[OUTDOOR_TEMP_DEV_START_ROW - 288:OUTDOOR_TEMP_DEV_START_ROW]
    ot_mean = float(np.mean(ot_pre))
    ot_std = float(np.std(ot_pre))
    outdoor_dev = ot_mean + 10 * ot_std  # very large deviation
    print(f"  Outdoor_temp dev: base_mean={ot_mean:.2f}, std={ot_std:.4f}, dev_value={outdoor_dev:.2f}")

    for i in range(OUTDOOR_TEMP_DEV_START_ROW, OUTDOOR_TEMP_DEV_START_ROW + OUTDOOR_TEMP_DEV_COUNT):
        rows[i][COL_OUTDOOR_TEMP] = _format_european_float(outdoor_dev, 2)

    # ----- 5. Psychrometric pair: consistent (air_temp + humidity) -----
    from src.models.psychrometric_validator import calculate_humidity_deficit

    pair_temp = 15.0
    pair_humidity = 90.0
    pair_hd = calculate_humidity_deficit(pair_temp, pair_humidity)
    print(f"\nPsychro consistent pair: rows {PSYCHRO_PAIR_START_ROW}-{PSYCHRO_PAIR_START_ROW + PSYCHRO_PAIR_COUNT}")
    print(f"  T={pair_temp}, RH={pair_humidity}, HD={pair_hd:.4f}")

    for i in range(PSYCHRO_PAIR_START_ROW, PSYCHRO_PAIR_START_ROW + PSYCHRO_PAIR_COUNT):
        rows[i][COL_GREENHOUSE_TEMP] = _format_european_float(pair_temp, 2)
        rows[i][COL_HUMIDITY] = _format_european_float(pair_humidity, 2)
        rows[i][COL_HUMIDITY_DEFICIT] = _format_european_float(pair_hd, 2)

    # ----- Write output -----
    out_lines = [header + "\n"]
    for r in rows:
        out_lines.append(";".join(r) + "\n")

    with open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as f:
        f.writelines(out_lines)

    print(f"\nWrote {len(rows)} rows to {OUTPUT_FILE.name}")

    # ----- Summary -----
    print("\n=== PARAMETERS FOR SYNTHETIC_FIXTURES.MD ===")
    print(f"Source: native_july_clearsky.csv rows {SRC_START}-{SRC_END} (all {SRC_END - SRC_START} rows)")
    print(f"Total output rows: {len(rows)}")
    print(f"Humidity freeze: rows {FREEZE_START_ROW}-{FREEZE_END_ROW-1}, value={FREEZE_VALUE}")
    print(f"  Duration: {FREEZE_LENGTH} rows = {FREEZE_LENGTH * 5 / 60:.2f} h")
    print(f"Release: row {FREEZE_END_ROW}")
    print(f"Suppression window ends: row {SUPPRESS_END_ROW}")
    print(f"Post-release deviations: rows {DEVIATION_START_ROW}-{DEVIATION_START_ROW + DEVIATION_COUNT - 1}")
    print(f"  deviation_val = {deviation_val}")
    print(f"  z_contam (first row, self-contaminated) = {z_contam:.4f}")
    print(f"  z_clean (first row) = {z_clean:.4f}")
    print(f"  Consecutive hits: {n_hits}/{DEVIATION_COUNT}")
    print(f"Burst inside window: rows {BURST_START_ROW}-{BURST_START_ROW + BURST_COUNT - 1}")
    print(f"Temp freeze: rows {TEMP_FREEZE_START_ROW}-{TEMP_FREEZE_END_ROW-1}, value={temp_freeze_val}")
    print(f"Outdoor_temp devs: rows {OUTDOOR_TEMP_DEV_START_ROW}-{OUTDOOR_TEMP_DEV_START_ROW + OUTDOOR_TEMP_DEV_COUNT - 1}")
    print(f"Psychro pair: rows {PSYCHRO_PAIR_START_ROW}-{PSYCHRO_PAIR_START_ROW + PSYCHRO_PAIR_COUNT - 1}")


if __name__ == "__main__":
    generate()

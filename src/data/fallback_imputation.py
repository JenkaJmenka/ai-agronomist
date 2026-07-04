"""
Fallback gap-filling for sensor time series with missing readings.

NOT part of the main ingestion pipeline.  Called only when an uploaded CSV
has gaps that would break the LSTM-VAE's fixed-length sequence requirement.

Every public function carries the ``fallback_`` prefix so callers can easily
distinguish synthetic fill from real measurements.

Two strategies are used depending on gap size:
  - Short gaps (≤ interpolate_threshold): linear interpolation between the
    two surrounding real readings.
  - Long gaps (> interpolate_threshold): greenhouse daily-baseline model —
    sinusoidal cycles fitted to typical operating ranges for each sensor.

Rows added by either strategy are tagged with ``_synthetic=True`` so
downstream code can filter or weight them differently.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

import pandas as pd

SENSOR_COLS = ["temperature", "humidity", "co2", "light"]

# Sinusoidal daily-cycle parameters: (lo, hi, peak_hour).
# Values match the realistic greenhouse seed profiles in src/db/seeds.py.
_BASELINE_PROFILES = {
    "temperature": (19.0, 28.0, 15.0),
    "humidity":    (55.0, 72.0,  4.0),
    "co2":         (380.0, 520.0, 3.0),
    "light":       (0.0, 950.0, 12.0),
}


# ── Private helpers ───────────────────────────────────────────────────────────

def _baseline_value(sensor: str, hour: float) -> float:
    lo, hi, peak_hour = _BASELINE_PROFILES[sensor]
    phase = (hour - peak_hour) / 24.0 * 2 * math.pi
    # cos(0) = 1 at peak_hour → maximum there; minimum 12h later.
    value = (lo + hi) / 2.0 + (hi - lo) / 2.0 * math.cos(phase)
    if sensor == "light" and (hour < 6.0 or hour >= 20.0):
        return 0.0
    return max(lo, min(hi, value))


def _sorted_ts(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["timestamp"]).sort_values().reset_index(drop=True)


def _synth_timestamps(
    gap_start: pd.Timestamp,
    gap_end: pd.Timestamp,
    interval: pd.Timedelta,
) -> pd.DatetimeIndex:
    return pd.date_range(start=gap_start + interval, end=gap_end - interval, freq=interval)


# ── Public fallback API ───────────────────────────────────────────────────────

def fallback_infer_interval(df: pd.DataFrame) -> pd.Timedelta:
    """Return the most common time delta between consecutive readings."""
    if len(df) < 2:
        return pd.Timedelta(minutes=5)
    diffs = _sorted_ts(df).diff().dropna()
    return diffs.mode().iloc[0]


def fallback_detect_gaps(
    df: pd.DataFrame,
    expected_interval: pd.Timedelta,
) -> pd.DataFrame:
    """Return a report of every gap larger than expected_interval.

    Columns: gap_start, gap_end, duration, missing_count.
    Returns an empty DataFrame when no gaps exist.
    """
    if df.empty or "timestamp" not in df.columns:
        return pd.DataFrame(columns=["gap_start", "gap_end", "duration", "missing_count"])

    ts = _sorted_ts(df)
    diffs = ts.diff().dropna()
    rows = []
    for idx in diffs[diffs > expected_interval].index:
        gap_start = ts.iloc[idx - 1]
        gap_end = ts.iloc[idx]
        duration = gap_end - gap_start
        rows.append({
            "gap_start": gap_start,
            "gap_end": gap_end,
            "duration": duration,
            "missing_count": max(0, int(duration / expected_interval) - 1),
        })
    return pd.DataFrame(rows)


def fallback_interpolate(
    df: pd.DataFrame,
    max_gap: pd.Timedelta,
    expected_interval: Optional[pd.Timedelta] = None,
) -> pd.DataFrame:
    """Fill gaps up to max_gap with linear interpolation between real readings.

    Gaps larger than max_gap are left untouched.  Synthetic rows carry
    ``_synthetic=True``.
    """
    if df.empty:
        return df

    interval = expected_interval or fallback_infer_interval(df)
    work = df.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])
    work = work.sort_values("timestamp").reset_index(drop=True)

    gaps = fallback_detect_gaps(work, interval)
    small_gaps = gaps[gaps["duration"] <= max_gap]
    if small_gaps.empty:
        return work

    fill_rows = []
    for _, gap in small_gaps.iterrows():
        synth_ts = _synth_timestamps(gap["gap_start"], gap["gap_end"], interval)
        if synth_ts.empty:
            continue
        before = work[work["timestamp"] <= gap["gap_start"]].iloc[-1]
        after = work[work["timestamp"] >= gap["gap_end"]].iloc[0]
        total_secs = (gap["gap_end"] - gap["gap_start"]).total_seconds()
        for ts in synth_ts:
            t = (ts - gap["gap_start"]).total_seconds() / total_secs
            row: dict = {"timestamp": ts, "_synthetic": True}
            for col in SENSOR_COLS:
                if col in work.columns:
                    row[col] = float(before[col]) + t * (float(after[col]) - float(before[col]))
            fill_rows.append(row)

    if not fill_rows:
        return work

    result = pd.concat([work, pd.DataFrame(fill_rows)], ignore_index=True)
    return result.sort_values("timestamp").reset_index(drop=True)


def fallback_fill_from_baseline(
    df: pd.DataFrame,
    expected_interval: Optional[pd.Timedelta] = None,
) -> pd.DataFrame:
    """Fill ALL remaining gaps using the programmed greenhouse daily baseline.

    Applies sinusoidal daily-cycle profiles for each sensor, matching typical
    greenhouse operating behaviour.  Use for gaps too large for interpolation
    (hours to days) where a straight line between real readings would be wrong.

    Synthetic rows carry ``_synthetic=True``.
    """
    if df.empty:
        return df

    interval = expected_interval or fallback_infer_interval(df)
    work = df.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])
    work = work.sort_values("timestamp").reset_index(drop=True)

    gaps = fallback_detect_gaps(work, interval)
    if gaps.empty:
        return work

    fill_rows = []
    for _, gap in gaps.iterrows():
        for ts in _synth_timestamps(gap["gap_start"], gap["gap_end"], interval):
            hour = ts.hour + ts.minute / 60.0
            row: dict = {"timestamp": ts, "_synthetic": True}
            for col in SENSOR_COLS:
                row[col] = _baseline_value(col, hour)
            fill_rows.append(row)

    if not fill_rows:
        return work

    result = pd.concat([work, pd.DataFrame(fill_rows)], ignore_index=True)
    return result.sort_values("timestamp").reset_index(drop=True)


def fallback_fill_gaps(
    df: pd.DataFrame,
    expected_interval: Optional[pd.Timedelta] = None,
    interpolate_threshold: pd.Timedelta = pd.Timedelta(hours=1),
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Main fallback entry point: detect gaps and fill them.

    Strategy per gap:
      ≤ interpolate_threshold  → linear interpolation between real neighbours
      > interpolate_threshold  → greenhouse daily-baseline model

    Returns:
        (filled_df, gap_report)
        gap_report columns: gap_start, gap_end, duration, missing_count, strategy
    """
    if df.empty:
        return df, pd.DataFrame()

    interval = expected_interval or fallback_infer_interval(df)
    gap_report = fallback_detect_gaps(df, interval)
    if gap_report.empty:
        return df, gap_report

    result = fallback_interpolate(df, max_gap=interpolate_threshold, expected_interval=interval)
    result = fallback_fill_from_baseline(result, expected_interval=interval)

    gap_report = gap_report.copy()
    gap_report["strategy"] = gap_report["duration"].apply(
        lambda d: "interpolate" if d <= interpolate_threshold else "baseline"
    )
    return result, gap_report

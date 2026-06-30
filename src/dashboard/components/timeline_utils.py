"""Pure timeline helpers without Streamlit dependencies."""

from __future__ import annotations

import datetime
from datetime import time as dt_time
from typing import Tuple

import pandas as pd

PAST_PRESET_DELTAS: dict[str, pd.Timedelta] = {
    "24h": pd.Timedelta(hours=24),
    "48h": pd.Timedelta(hours=48),
    "3d": pd.Timedelta(days=3),
    "5d": pd.Timedelta(days=5),
    "7d": pd.Timedelta(days=7),
    "1m": pd.Timedelta(days=30),
    "3m": pd.Timedelta(days=90),
    "6m": pd.Timedelta(days=180),
}
PAST_PRESET_DEFAULT = "7d"


def combine_date_and_time(date_value: datetime.date, time_value: dt_time) -> datetime.datetime:
    return datetime.datetime.combine(date_value, time_value)


def _to_timestamp(value: datetime.datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        return ts
    return ts.tz_localize(None)


def _align_timestamp(ts: pd.Timestamp, reference: pd.Timestamp) -> pd.Timestamp:
    ref = _to_timestamp(reference)
    value = _to_timestamp(ts)
    if ref.tzinfo is not None:
        if value.tzinfo is None:
            return value.tz_localize("UTC")
        return value
    if value.tzinfo is not None:
        return value.tz_localize(None)
    return value


def default_seven_day_window(
    data_min: pd.Timestamp,
    data_max: pd.Timestamp,
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """Last 7 calendar days ending on the data's last day (00:00 → 23:55)."""
    data_min = _to_timestamp(data_min)
    data_max = _to_timestamp(data_max)
    end_day = data_max.normalize()
    end = end_day + pd.Timedelta(hours=23, minutes=55)
    if end > data_max:
        end = data_max
    start = end_day - pd.Timedelta(days=6)
    start = max(start, data_min.normalize())
    return clamp_timeline_bounds(start, end, data_min, data_max)


def window_from_past_preset(
    preset: str,
    data_min: pd.Timestamp,
    data_max: pd.Timestamp,
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """Rolling window ending at the latest data timestamp."""
    delta = PAST_PRESET_DELTAS.get(preset, PAST_PRESET_DELTAS[PAST_PRESET_DEFAULT])
    end = _to_timestamp(data_max)
    start = end - delta
    start = max(start, _to_timestamp(data_min))
    return clamp_timeline_bounds(start, end, data_min, data_max)


def clamp_timeline_bounds(
    start: pd.Timestamp,
    end: pd.Timestamp,
    data_min: pd.Timestamp,
    data_max: pd.Timestamp,
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """Keep the selected window inside loaded data bounds."""
    data_min = _to_timestamp(data_min)
    data_max = _to_timestamp(data_max)
    start = _align_timestamp(start, data_min)
    end = _align_timestamp(end, data_min)

    if start > end:
        start, end = end, start
    if start < data_min:
        start = data_min
    if end > data_max:
        end = data_max
    if start > end:
        start, end = data_min, data_max
    return start, end

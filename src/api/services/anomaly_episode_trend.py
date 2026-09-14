"""Trend classification and headline text for query-time anomaly episodes.

No I/O, no pandas.  See ``anomaly_episode_service.py`` for the module-level
grouping rules this supports.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from src.api.services.anomaly_episode_rows import Direction
from src.api.services.fault_classification import (
    _SENSOR_BOUNDS,
    _simple_reasoning_from_values,
)

Trend = Literal["worsening", "easing", "flat"]

_SECONDS_PER_HOUR = 3600.0


def _trend_from_values(
    times: list[datetime],
    values: list[float],
    *,
    channel: str,
    direction: Direction,
    deadband: float,
) -> tuple[Trend, float | None]:
    """Least-squares slope of the trigger value, oriented by direction.

    For a ``drop``, a falling slope is worsening; for a ``spike``, a rising
    slope is worsening.  Without a known direction (in-band trigger value, or
    an unbounded channel) there is no notion of "bad" direction, so the
    result is always "flat" — guessing would misinform the operator.  Slope
    magnitude below ``deadband`` (fraction of the channel's band span per
    hour) also reads as "flat" so sensor noise doesn't flip the label.
    """
    if len(times) < 2 or direction is None:
        return "flat", None

    t0 = times[0]
    xs = [(t - t0).total_seconds() / _SECONDS_PER_HOUR for t in times]
    ys = values

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return "flat", None

    slope = numerator / denominator  # value units per hour
    if not math.isfinite(slope):
        return "flat", None

    bounds = _SENSOR_BOUNDS.get(channel)
    span = (bounds[1] - bounds[0]) if bounds is not None else abs(mean_y) or 1.0
    if abs(slope) <= deadband * span:
        return "flat", slope

    if direction == "drop":
        trend: Trend = "worsening" if slope < 0 else "easing"
    else:  # "spike"
        trend = "worsening" if slope > 0 else "easing"
    return trend, slope


def _format_duration(duration_seconds: float) -> str:
    if duration_seconds <= 0:
        return "a single reading"
    minutes = duration_seconds / 60.0
    if minutes < 60:
        return f"{minutes:.0f} min"
    return f"{minutes / 60.0:.1f} hr"


def _build_headline(
    channel: str, peak_value: float | None, duration_seconds: float, trend: Trend,
) -> str:
    """One sentence: the extremum reasoning plus duration and trend."""
    reasoning = _simple_reasoning_from_values(channel, peak_value)
    duration_text = _format_duration(duration_seconds)
    if duration_seconds <= 0:
        return f"{reasoning} ({duration_text})"
    return f"{reasoning}, {trend} over {duration_text}"

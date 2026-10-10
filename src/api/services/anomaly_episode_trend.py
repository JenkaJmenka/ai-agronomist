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
Movement = Literal["rising", "falling", "steady"]

_SECONDS_PER_HOUR = 3600.0


def _slope_per_hour(times: list[datetime], values: list[float]) -> float | None:
    """Least-squares slope of the trigger value, in value units per hour."""
    if len(times) < 2:
        return None

    t0 = times[0]
    xs = [(t - t0).total_seconds() / _SECONDS_PER_HOUR for t in times]

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None

    slope = numerator / denominator
    return slope if math.isfinite(slope) else None


def _movement_from_slope(
    slope: float, values: list[float], *, channel: str, deadband: float,
) -> Movement:
    """Which way the channel moved, with a deadband so noise reads steady.

    The deadband is a fraction of the channel's band span per hour; a channel
    with no configured band falls back to its own mean magnitude so the
    threshold stays proportional to the units in play.
    """
    bounds = _SENSOR_BOUNDS.get(channel)
    if bounds is not None:
        span = bounds[1] - bounds[0]
    else:
        span = abs(sum(values) / len(values)) or 1.0
    if abs(slope) <= deadband * span:
        return "steady"
    return "rising" if slope > 0 else "falling"


def _episode_dynamics(
    times: list[datetime],
    values: list[float],
    *,
    channel: str,
    direction: Direction,
    deadband: float,
) -> tuple[Trend, Movement, float | None]:
    """Classify an episode's value series as a movement and, if possible, a trend.

    ``movement`` is factual — the channel rose, fell, or held steady — and is
    available for every channel, including SCADA columns with no configured
    band.  ``trend`` is the band-relative reading (is this getting *worse*?),
    which only means anything once ``direction`` says which side of the band
    was breached; without it the trend stays "flat" rather than guessing which
    way is bad.

    Reporting only the trend made every unbounded channel read "steady" even
    while it moved sharply — `calculated_heating_temp` climbing 18.4 -> 21.7
    was labelled flat.  The movement carries that signal instead.
    """
    slope = _slope_per_hour(times, values)
    if slope is None:
        return "flat", "steady", None

    movement = _movement_from_slope(slope, values, channel=channel, deadband=deadband)
    if direction is None or movement == "steady":
        return "flat", movement, slope

    if direction == "drop":
        trend: Trend = "worsening" if movement == "falling" else "easing"
    else:  # "spike"
        trend = "worsening" if movement == "rising" else "easing"
    return trend, movement, slope


def _format_duration(duration_seconds: float) -> str:
    if duration_seconds <= 0:
        return "a single reading"
    minutes = duration_seconds / 60.0
    if minutes < 60:
        return f"{minutes:.0f} min"
    return f"{minutes / 60.0:.1f} hr"


def _build_headline(
    channel: str,
    peak_value: float | None,
    duration_seconds: float,
    trend: Trend,
    movement: Movement = "steady",
) -> str:
    """One sentence: the extremum reasoning plus duration and how it developed.

    Prefers the band-relative trend when there is one; otherwise falls back to
    the factual movement, so an unbounded channel reads "rising over 45 min"
    rather than the misleading "flat".
    """
    reasoning = _simple_reasoning_from_values(channel, peak_value)
    duration_text = _format_duration(duration_seconds)
    if duration_seconds <= 0:
        return f"{reasoning} ({duration_text})"
    development = trend if trend != "flat" else movement
    return f"{reasoning}, {development} over {duration_text}"

"""Pure grouping core for query-time anomaly episode merging.

No I/O, no pandas — every function here is unit-testable without a database.
Per-row field derivation lives in ``anomaly_episode_rows.py``; trend and
headline text live in ``anomaly_episode_trend.py``.  See
``anomaly_episode_service.py`` for the module-level grouping rules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.api.services.anomaly_episode_rows import (
    Direction,
    _RowInfo,
    _build_row_infos,
    _finite_or_none,
)
from src.api.services.anomaly_episode_trend import _build_headline, _trend_from_values
from src.api.services.sample_enrichment import _iso
from src.config import ANOMALY_EPISODE_GAP_SECONDS, ANOMALY_GROUP_SAMPLE_IDS, ANOMALY_TREND_FLAT_DEADBAND
from src.models.episode_grouper import _severity_rank


def build_episodes(
    row_dicts: list[dict[str, Any]],
    *,
    gap_seconds: int = ANOMALY_EPISODE_GAP_SECONDS,
    deadband: float = ANOMALY_TREND_FLAT_DEADBAND,
    sample_limit: int = ANOMALY_GROUP_SAMPLE_IDS,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Group flagged rows into contiguous (channel, detector, direction) episodes.

    Same sort-then-scan shape as
    ``episode_grouper.group_alerts_into_episodes``.

    Each returned dict carries an internal ``"sample_row_dicts"`` key — the
    episode's top-``sample_limit`` raw rows by score, unenriched.  The caller
    (``anomaly_episode_service.fetch_anomaly_episodes``) batch-enriches those
    across all episodes and replaces the key with ``"samples"``.

    Must be called with EVERY flagged row in the window — never a top-N
    subset (see anomaly_episode_service module docstring).

    ``window_start``/``window_end`` are the *queried* window bounds, used only
    to set the ``is_truncated_*`` flags.  They must not be derived from the
    rows themselves: the earliest flagged row in a window is almost never the
    window boundary, so comparing against min/max of the data would mark the
    first and last episode of every query as truncated regardless of whether
    anything was actually cut off.  When omitted, both flags are False.
    """
    infos = _build_row_infos(row_dicts)
    if not infos:
        return []

    infos.sort(key=lambda info: ((info.channel, info.detector, info.direction), info.dt))

    episodes: list[dict[str, Any]] = []
    current_key: tuple[str, str, Direction] | None = None
    current_group: list[_RowInfo] = []

    for info in infos:
        key = (info.channel, info.detector, info.direction)
        if key != current_key:
            _flush_episode(
                current_group, episodes, deadband, sample_limit,
                window_start, window_end,
            )
            current_key = key
            current_group = [info]
        else:
            gap = (info.dt - current_group[-1].dt).total_seconds()
            if gap > gap_seconds:
                _flush_episode(
                    current_group, episodes, deadband, sample_limit,
                    window_start, window_end,
                )
                current_group = [info]
            else:
                current_group.append(info)

    _flush_episode(
        current_group, episodes, deadband, sample_limit, window_start, window_end,
    )
    return episodes


def _episode_extremum(
    values: list[float], direction: Direction, *, fallback: float | None,
) -> float | None:
    """The worst value reached during the episode, not the worst-scoring one.

    An operator reading "humidity drop: 32.0%" when the reading actually
    bottomed at 29.0% is being misinformed — the value at the highest-scoring
    row is not the value the event is remembered by.  Min for a drop, max for
    a spike.  Without a direction there is no "worst", so the caller's
    score-based value stands.
    """
    if not values or direction is None:
        return fallback
    return min(values) if direction == "drop" else max(values)


def _flush_episode(
    group: list[_RowInfo],
    episodes: list[dict[str, Any]],
    deadband: float,
    sample_limit: int,
    window_start: datetime | None,
    window_end: datetime | None,
) -> None:
    """Convert a contiguous group of rows into an episode dict."""
    if not group:
        return

    channel = group[0].channel
    detector = group[0].detector
    direction = group[0].direction

    start_dt = group[0].dt
    end_dt = group[-1].dt
    duration_seconds = (end_dt - start_dt).total_seconds() if len(group) > 1 else 0.0

    peak_idx = max(range(len(group)), key=lambda i: group[i].score)
    peak_info = group[peak_idx]

    severities = [info.row.get("detected_severity") for info in group]
    severity = max(severities, key=_severity_rank) or "normal"

    valued = [(info.dt, info.value) for info in group if info.value is not None]
    trend, slope_per_hour = _trend_from_values(
        [dt for dt, _ in valued], [v for _, v in valued],
        channel=channel, direction=direction, deadband=deadband,
    )

    peak_value = _episode_extremum(
        [v for _, v in valued], direction, fallback=peak_info.value,
    )

    affected_sensors: list[str] = []
    for info in group:
        for sensor in info.affected_sensors:
            if sensor not in affected_sensors:
                affected_sensors.append(sensor)

    headline = _build_headline(channel, peak_value, duration_seconds, trend)

    ranked = sorted(group, key=lambda info: info.score, reverse=True)
    top_rows = sorted(ranked[:sample_limit], key=lambda info: info.dt, reverse=True)

    episodes.append({
        "episode_id": f"{channel}:{detector}:{direction}:{_iso(start_dt)}",
        "channel": channel,
        "detector": detector,
        "direction": direction,
        "start_ts": _iso(start_dt),
        "end_ts": _iso(end_dt),
        "duration_seconds": duration_seconds,
        "sample_count": len(group),
        "peak_score": _finite_or_none(peak_info.score) or 0.0,
        "peak_ts": _iso(peak_info.dt),
        "severity": severity,
        "first_value": _finite_or_none(group[0].value),
        "last_value": _finite_or_none(group[-1].value),
        "peak_value": _finite_or_none(peak_value),
        "trend": trend,
        "slope_per_hour": _finite_or_none(slope_per_hour),
        "affected_sensors": affected_sensors,
        "headline": headline,
        "is_truncated_start": window_start is not None and start_dt <= window_start,
        "is_truncated_end": window_end is not None and end_dt >= window_end,
        "sample_row_dicts": [info.row for info in top_rows],
    })

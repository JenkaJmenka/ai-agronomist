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
from src.api.services.anomaly_episode_trend import _build_headline, _episode_dynamics
from src.api.services.fault_classification import _SENSOR_BOUNDS, _UNKNOWN_KEY
from src.api.services.sample_enrichment import _iso
from src.config import (
    ANOMALY_EPISODE_DEGRADED_VALUE_SPAN_FRACTION,
    ANOMALY_EPISODE_GAP_SECONDS,
    ANOMALY_GROUP_SAMPLE_IDS,
    ANOMALY_TREND_FLAT_DEADBAND,
)
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

    Rows whose ``trigger_detector``/direction were never persisted (LSTM-only
    score backfill, live ingestion) degrade to ``detector="unknown"``,
    ``direction=None`` — a key that, alone, cannot distinguish two unrelated
    in-band excursions on the same channel.  When BOTH components are
    degraded on a contiguous run, ``_degraded_value_span_exceeded`` also caps
    how far the group's value may drift before splitting, so an all-day
    in-band ramp (e.g. the light channel's diurnal curve) cannot present as
    one multi-hour episode just because nothing ever breached a bound.
    """
    infos = _build_row_infos(row_dicts)
    if not infos:
        return []

    # direction is None for in-band values, and None is not orderable against
    # str — sorting a window that mixes both raises TypeError.  Substitute ""
    # for ordering only; the grouping key below still compares the real value,
    # so a None-direction run stays its own episode.
    infos.sort(
        key=lambda info: (info.channel, info.detector, info.direction or "", info.dt)
    )

    episodes: list[dict[str, Any]] = []
    current_key: tuple[str, str, Direction] | None = None
    current_group: list[_RowInfo] = []

    for info in infos:
        key = (info.channel, info.detector, info.direction)
        must_split = key != current_key
        if not must_split:
            gap = (info.dt - current_group[-1].dt).total_seconds()
            must_split = gap > gap_seconds
        if not must_split and _is_degraded_key(info.detector, info.direction):
            must_split = _degraded_value_span_exceeded(
                current_group, info.value, info.channel,
            )
        if must_split:
            _flush_episode(
                current_group, episodes, deadband, sample_limit,
                window_start, window_end,
            )
            current_key = key
            current_group = [info]
        else:
            current_group.append(info)

    _flush_episode(
        current_group, episodes, deadband, sample_limit, window_start, window_end,
    )
    return episodes


def _is_degraded_key(detector: str, direction: Direction) -> bool:
    """True when neither the detector nor the direction is real evidence.

    ``detector == "unknown"`` only happens for rows whose ``trigger_detector``
    was never persisted (see module docstring); ``direction is None`` also
    happens legitimately for real detector rows whose value never breaches a
    configured band (an LSTM multivariate hit, say).  Only the conjunction of
    both is the degenerate case this guards against — a single missing
    component still leaves one real signal in the key.
    """
    return detector == _UNKNOWN_KEY and direction is None


def _degraded_value_span_exceeded(
    group: list[_RowInfo],
    candidate_value: float | None,
    channel: str,
    fraction: float = ANOMALY_EPISODE_DEGRADED_VALUE_SPAN_FRACTION,
) -> bool:
    """True if adding ``candidate_value`` widens a degraded group past its cap.

    Every row already in ``group`` shares an in-band value for ``channel``
    (that is what put them on a ``direction=None`` key), so the group's value
    envelope is bounded by the channel's configured band already — this just
    refuses to let a degraded-key run consume more than ``fraction`` of it.
    A channel with no configured band (``_SENSOR_BOUNDS`` has no entry — e.g.
    an unresolved channel or a SCADA column) has nothing to measure drift
    against, so the check is skipped and time-gap contiguity alone decides.
    """
    if candidate_value is None:
        return False
    bounds = _SENSOR_BOUNDS.get(channel)
    if bounds is None:
        return False
    span = bounds[1] - bounds[0]
    if span <= 0:
        return False
    values = [info.value for info in group if info.value is not None]
    values.append(candidate_value)
    if len(values) < 2:
        return False
    return (max(values) - min(values)) > fraction * span


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
    severity = max(severities, key=_severity_rank) or "unclassified"

    valued = [(info.dt, info.value) for info in group if info.value is not None]
    trend, movement, slope_per_hour = _episode_dynamics(
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

    headline = _build_headline(channel, peak_value, duration_seconds, trend, movement)

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
        "movement": movement,
        "slope_per_hour": _finite_or_none(slope_per_hour),
        "affected_sensors": affected_sensors,
        "headline": headline,
        "is_truncated_start": window_start is not None and start_dt <= window_start,
        "is_truncated_end": window_end is not None and end_dt >= window_end,
        "sample_row_dicts": [info.row for info in top_rows],
    })

"""Per-row field derivation for query-time anomaly episode merging.

No I/O, no pandas.  Resolves each flagged row's (channel, detector,
direction) grouping key and display value, handling rows that pre-date
migration 024 (NULL trigger_channel/detector).  See
``anomaly_episode_service.py`` for the module-level grouping rules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from src.api.services.fault_classification import (
    _UNKNOWN_KEY,
    _canonical_sensor_value,
    _direction_from_value,
    _infer_sensors_from_values,
)
from src.models.episode_grouper import _parse_iso

Direction = Literal["drop", "spike"] | None


@dataclass
class _RowInfo:
    """A flagged row plus the fields derived from it for grouping/display."""

    row: dict[str, Any]
    dt: datetime
    channel: str
    detector: str
    direction: Direction
    value: float | None
    score: float
    affected_sensors: list[str] = field(default_factory=list)


def _row_channel_and_detector(row: dict[str, Any]) -> tuple[str, str]:
    """Resolve (channel, detector) for a row, handling pre-024 NULLs.

    Rows written before migration 024 have NULL trigger_channel/detector.
    Falls back to value-based sensor inference, then to the unknown key —
    mirrors the fallback chain in anomaly_group_service._bucket_rows_by_group.
    """
    trigger_channel = row.get("trigger_channel")
    if trigger_channel:
        channel = str(trigger_channel)
    else:
        inferred = _infer_sensors_from_values(
            row.get("temperature"), row.get("humidity"),
            row.get("co2"), row.get("light"),
        )
        channel = inferred[0] if inferred else _UNKNOWN_KEY

    detector = str(row.get("trigger_detector") or _UNKNOWN_KEY)
    return channel, detector


def _row_value(row: dict[str, Any], channel: str) -> float | None:
    """The channel's value at this row — persisted trigger_value first."""
    trigger_value = row.get("trigger_value")
    if trigger_value is not None:
        return float(trigger_value)
    return _canonical_sensor_value(
        channel, row.get("temperature"), row.get("humidity"),
        row.get("co2"), row.get("light"),
    )


def _finite_or_none(value: float | None) -> float | None:
    """Guard a float before it reaches a JSON response.

    SQLite silently accepts NaN/inf in a JSON column; Postgres 500s on it
    (project_sqlite_postgres_nan_gap).
    """
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return None
    return as_float if math.isfinite(as_float) else None


def _build_row_infos(row_dicts: list[dict[str, Any]]) -> list[_RowInfo]:
    """Derive grouping/display fields for every row, skipping unparseable rows."""
    infos: list[_RowInfo] = []
    for row in row_dicts:
        ts = row.get("timestamp")
        if not ts:
            continue
        try:
            dt = _parse_iso(str(ts))
        except ValueError:
            continue

        channel, detector = _row_channel_and_detector(row)
        value = _row_value(row, channel)
        direction = _direction_from_value(channel, value)
        raw_score = row.get("anomaly_score")
        score = float(raw_score) if raw_score is not None else 0.0
        affected_sensors = _infer_sensors_from_values(
            row.get("temperature"), row.get("humidity"),
            row.get("co2"), row.get("light"),
        )

        infos.append(_RowInfo(
            row=row, dt=dt, channel=channel, detector=detector,
            direction=direction, value=value, score=score,
            affected_sensors=affected_sensors,
        ))
    return infos

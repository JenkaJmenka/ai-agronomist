"""Compute seasonal telemetry baselines (mean / std / median) per sensor.

Filters ``SensorReading`` rows by calendar months mapped to summer/winter
via the tenant's operational ``season_map``. Median is computed in numpy
after a capped / strided fetch so SQLite and Postgres stay portable.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

import numpy as np
from sqlalchemy import extract, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import DEFAULT_SEASON_MAP, SEASONAL_BASELINE_ROW_CAP, SENSOR_NAMES
from src.db.models import SensorReading

logger = logging.getLogger(__name__)

_SENSORS: tuple[str, ...] = tuple(SENSOR_NAMES)


def months_for_season(
    season_map: dict[str, str],
    season: str,
) -> list[int]:
    """Return calendar months (1–12) mapped to *season*."""
    return sorted(
        int(month)
        for month, mapped in season_map.items()
        if str(mapped).lower() == season
    )


async def compute_seasonal_baselines(
    session: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    season_map: dict[str, str] | None = None,
    row_cap: int = SEASONAL_BASELINE_ROW_CAP,
) -> dict[str, Any]:
    """Return global + per-season stats for the four model sensors.

    Shape::

        {
          "global": {sensor: {mean, std, median, n}},
          "seasons": {
            "summer": {sensor: {mean, std, median, n}},
            "winter": {sensor: {mean, std, median, n}},
          },
          "season_map": {...},
        }
    """
    smap = dict(season_map or DEFAULT_SEASON_MAP)
    rows = await _fetch_readings(
        session,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        row_cap=row_cap,
    )
    global_stats = _stats_from_rows(rows)
    seasons: dict[str, dict[str, Any]] = {}
    for season in ("summer", "winter"):
        months = set(months_for_season(smap, season))
        season_rows = [r for r in rows if r["month"] in months]
        seasons[season] = _stats_from_rows(season_rows)

    return {
        "global": global_stats,
        "seasons": seasons,
        "season_map": smap,
        "row_count": len(rows),
    }


async def _fetch_readings(
    session: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    row_cap: int,
) -> list[dict[str, Any]]:
    """Fetch up to *row_cap* readings (newest first, then reverse chronological)."""
    stmt = (
        select(
            SensorReading.timestamp,
            SensorReading.temperature,
            SensorReading.humidity,
            SensorReading.co2,
            SensorReading.light,
            extract("month", SensorReading.timestamp).label("month"),
        )
        .where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
        )
        .order_by(SensorReading.timestamp.desc())
        .limit(max(row_cap, 1))
    )
    result = await session.execute(stmt)
    out: list[dict[str, Any]] = []
    for row in result.all():
        out.append(
            {
                "month": int(row.month),
                "temperature": float(row.temperature),
                "humidity": float(row.humidity),
                "co2": float(row.co2),
                "light": float(row.light),
            }
        )
    out.reverse()
    # Even stride if somehow over-cap (defensive).
    if len(out) > row_cap:
        stride = len(out) / row_cap
        out = [out[int(i * stride)] for i in range(row_cap)]
    return out


def _stats_from_rows(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    stats: dict[str, dict[str, float | int]] = {}
    for sensor in _SENSORS:
        values = np.asarray(
            [r[sensor] for r in rows if np.isfinite(r.get(sensor, np.nan))],
            dtype=float,
        )
        if values.size == 0:
            stats[sensor] = {"mean": 0.0, "std": 0.0, "median": 0.0, "n": 0}
            continue
        stats[sensor] = {
            "mean": float(np.mean(values)),
            "std": float(np.std(values, ddof=0)),
            "median": float(np.median(values)),
            "n": int(values.size),
        }
    return stats

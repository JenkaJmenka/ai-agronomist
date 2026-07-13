"""Build per-tenant diurnal (hourly) sensor profiles from stored history.

The diurnal profile detector (spec §9) needs per-hour mean/std baselines,
but nothing populated them — the detector was effectively disabled for
every tenant. This service builds profiles lazily from the tenant's own
``sensor_readings`` history and persists them into
``LearningState.state_json["diurnal_profiles"]`` (the key the pipeline
already restores from), so the detector auto-enables once a tenant has
enough data. Profiles are keyed per tenant, matching ``LearningState``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import (
    DIURNAL_PROFILE_HISTORY_DAYS,
    DIURNAL_PROFILE_MIN_ROWS,
    SENSOR_NAMES,
)
from src.db.models import LearningState, SensorReading

logger = logging.getLogger(__name__)

HourlyStats = Dict[int, tuple]
DiurnalProfiles = Dict[str, HourlyStats]

# Minimum readings within one hour bucket before its stats are trusted.
_MIN_SAMPLES_PER_HOUR: int = 5


def compute_hourly_stats(timestamps, values) -> HourlyStats:
    """Build per-hour (mean, std) from timestamped sensor readings.

    Hours with fewer than ``_MIN_SAMPLES_PER_HOUR`` samples are omitted;
    std is floored at 1e-6 so downstream Z-scores never divide by zero.
    """
    import numpy as np
    from collections import defaultdict

    buckets: Dict[int, list] = defaultdict(list)
    for ts, val in zip(timestamps, values):
        if val is not None and not (isinstance(val, float) and np.isnan(val)):
            buckets[ts.hour].append(val)

    stats: HourlyStats = {}
    for hour, vals in buckets.items():
        if len(vals) >= _MIN_SAMPLES_PER_HOUR:
            arr = np.array(vals)
            stats[hour] = (float(np.mean(arr)), max(float(np.std(arr)), 1e-6))
    return stats


async def build_diurnal_profiles(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str = "default",
) -> DiurnalProfiles:
    """Hourly (mean, std) per sensor from recent canonical readings.

    Returns {} when the tenant has fewer than ``DIURNAL_PROFILE_MIN_ROWS``
    readings in the trailing window — too little history for hourly
    baselines to mean anything.
    """
    since = datetime.now(timezone.utc) - timedelta(days=DIURNAL_PROFILE_HISTORY_DAYS)
    stmt = select(SensorReading).where(
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
        SensorReading.timestamp >= since,
    )
    rows = (await db.execute(stmt)).scalars().all()
    if len(rows) < DIURNAL_PROFILE_MIN_ROWS:
        return {}

    timestamps = [row.timestamp for row in rows]
    profiles: DiurnalProfiles = {}
    for sensor in SENSOR_NAMES:
        stats = compute_hourly_stats(
            timestamps, [getattr(row, sensor, None) for row in rows]
        )
        if stats:
            profiles[sensor] = stats
    return profiles


async def persist_diurnal_profiles(
    db: AsyncSession,
    *,
    tenant_id: str,
    profiles: DiurnalProfiles,
) -> None:
    """Merge profiles into LearningState.state_json (JSON-safe keys)."""
    serialized = {
        sensor: {str(hour): list(stat) for hour, stat in hours.items()}
        for sensor, hours in profiles.items()
    }
    row = await db.get(LearningState, tenant_id)
    if row is None:
        row = LearningState(customer_id=tenant_id, state_json={})
        db.add(row)
    row.state_json = {
        **(row.state_json or {}),
        "diurnal_profiles": serialized,
        "diurnal_profiles_built_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.commit()
    logger.info(
        "Persisted diurnal profiles for tenant %s (%d sensors)",
        tenant_id,
        len(profiles),
    )

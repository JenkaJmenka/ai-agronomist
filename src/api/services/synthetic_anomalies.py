"""Synthetic anomaly generation and cleanup behind the inject-test routes.

Injection learns column names and value ranges from existing RawReadings so
injected spikes match the user's real sensors, then writes SensorReading rows
AND RawReading rows carrying the same provenance marker:
``source = "synthetic:<type>"`` for the anomalous rows, ``"synthetic:context"``
for the normal filler rows in between. ``anomaly_type`` on SensorReading still
records the injected label (or "synthetic_context" for filler), but it is no
longer what removal keys off — see ``delete_synthetic_anomalies`` below and
``docs/architecture/ADR-010-synthetic-provenance-marker.md``. Removal only
touches rows carrying the ``synthetic:`` source marker — real sensor data,
including future real detections that will set ``anomaly_type`` once Task 3
lands, is never deleted.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.cache_invalidation import invalidate_response_cache
from src.api.services.synthetic_anomaly_generators import (
    ANOMALY_TYPES,
    # _TYPE_PROFILES and _pick_col are not used below -- they are re-exported
    # on purpose: tests/api/test_synthetic_anomalies_gas_isolation.py imports
    # this module (not synthetic_anomaly_generators) and reaches them as
    # synthetic_anomalies_service._TYPE_PROFILES / ._pick_col. Removing this
    # import keeps the code working but breaks that test's import path.
    _TYPE_PROFILES,
    _build_injected_row,
    _col_ranges_from_sample,
    _drift_rank_for,
    _InjectionRunContext,
    _pick_col,
    _resolve_raw_spike_cols,
    _type_config_for,
)
from src.config import (
    SYNTHETIC_CANONICAL_STATS,
    SYNTHETIC_CONTEXT_SCORE_RANGE,
    SYNTHETIC_DEFAULT_WINDOW_HOURS,
    SYNTHETIC_MIN_INTERVAL_SECONDS,
    SYNTHETIC_SAMPLE_SIZE,
    SYNTHETIC_SOURCE_PREFIX,
    synthetic_source,
)
from src.db.models import RawReading, SensorReading


def _resolve_window(
    window_start: Optional[str], window_end: Optional[str]
) -> tuple[datetime, datetime]:
    """Parse the requested injection window; default to the last
    SYNTHETIC_DEFAULT_WINDOW_HOURS hours."""
    if not (window_start and window_end):
        end_dt = datetime.now(timezone.utc)
        return end_dt - timedelta(hours=SYNTHETIC_DEFAULT_WINDOW_HOURS), end_dt
    try:
        start_dt = datetime.fromisoformat(window_start)
        end_dt = datetime.fromisoformat(window_end)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid window datetime: {exc}")
    if end_dt <= start_dt:
        raise HTTPException(status_code=422, detail="window_end must be after window_start")
    return start_dt, end_dt


async def _resolve_sample_stats(
    db: AsyncSession, tenant_id: str, greenhouse_id: str
) -> tuple[dict, bool]:
    """Learn column ranges from up to SYNTHETIC_SAMPLE_SIZE recent real rows.

    Falls back to the canonical stats when the tenant has no RawReading
    history yet (``has_raw_cols=False`` tells the caller not to write sibling
    RawReading rows for this run).
    """
    sample_rows = (
        await db.execute(
            select(RawReading)
            .where(RawReading.tenant_id == tenant_id, RawReading.greenhouse_id == greenhouse_id)
            .order_by(RawReading.timestamp.desc())
            .limit(SYNTHETIC_SAMPLE_SIZE)
        )
    ).scalars().all()

    col_stats = _col_ranges_from_sample(sample_rows)
    has_raw_cols = bool(col_stats)
    if not col_stats:
        col_stats = dict(SYNTHETIC_CANONICAL_STATS)
    return col_stats, has_raw_cols


async def inject_synthetic_anomalies(
    db: AsyncSession,
    *,
    tenant_id: str, greenhouse_id: str, n_normal: int, n_anomalies: int,
    anomaly_type: str, window_start: Optional[str] = None, window_end: Optional[str] = None,
) -> dict:
    """Insert synthetic readings and return the injection summary payload."""
    if anomaly_type not in ANOMALY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown anomaly_type '{anomaly_type}'. Valid: {ANOMALY_TYPES}",
        )

    start_dt, end_dt = _resolve_window(window_start, window_end)
    window_hours = max(1, int((end_dt - start_dt).total_seconds() / 3600))
    total = n_normal + n_anomalies
    interval_seconds = max(
        SYNTHETIC_MIN_INTERVAL_SECONDS,
        int((end_dt - start_dt).total_seconds() / max(total, 1)),
    )

    col_stats, has_raw_cols = await _resolve_sample_stats(db, tenant_id, greenhouse_id)
    type_config = _type_config_for(anomaly_type)
    raw_spike_cols = _resolve_raw_spike_cols(col_stats, type_config.raw_keywords)
    anomaly_positions = set(random.sample(range(total), min(n_anomalies, total)))
    drift_rank = _drift_rank_for(anomaly_type, anomaly_positions)

    context = _InjectionRunContext(
        tenant_id=tenant_id, greenhouse_id=greenhouse_id, anomaly_type=anomaly_type,
        type_config=type_config, col_stats=col_stats, raw_spike_cols=raw_spike_cols,
        has_raw_cols=has_raw_cols, ctx_score_range=SYNTHETIC_CONTEXT_SCORE_RANGE,
        start_dt=start_dt, interval_seconds=interval_seconds,
        anomaly_positions=anomaly_positions, drift_rank=drift_rank,
    )
    sensor_records, raw_records = _assemble_injected_rows(context, total)
    await _persist_injection_results(db, sensor_records, raw_records)

    return {
        "injected": len(sensor_records),
        "raw_injected": len(raw_records),
        "anomalies": n_anomalies,
        "normal": n_normal,
        "anomaly_type": anomaly_type,
        "columns_used": list(col_stats.keys()),
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
        "window_start": start_dt.isoformat(),
        "window_end": end_dt.isoformat(),
        "window_hours": window_hours,
    }


def _assemble_injected_rows(
    context: "_InjectionRunContext", total: int
) -> tuple[list[SensorReading], list[RawReading]]:
    """Build every (sensor, raw) row pair for one injection run."""
    sensor_records: list[SensorReading] = []
    raw_records: list[RawReading] = []
    for i in range(total):
        sensor_row, raw_row = _build_injected_row(context, i)
        sensor_records.append(sensor_row)
        if raw_row is not None:
            raw_records.append(raw_row)
    return sensor_records, raw_records


async def _persist_injection_results(
    db: AsyncSession, sensor_records: list[SensorReading], raw_records: list[RawReading]
) -> None:
    """Write the injected rows and invalidate the cached readings/anomalies responses."""
    db.add_all(sensor_records)
    if raw_records:
        db.add_all(raw_records)
    await db.commit()
    await invalidate_response_cache("inject_test_anomalies")


async def delete_synthetic_anomalies(
    db: AsyncSession, *, tenant_id: str, greenhouse_id: str, anomaly_type: Optional[str]
) -> dict:
    """Remove synthetically injected test anomalies. Real sensor data is never touched."""
    # Treat empty/whitespace-only string the same as omit (UI "All synthetic"
    # option). The or-None fallback must run after strip(), or a
    # whitespace-only value survives stripping as "" and then fails the
    # ANOMALY_TYPES membership check below with a confusing 422.
    resolved_type = anomaly_type.strip() if anomaly_type else None
    resolved_type = resolved_type or None
    if resolved_type is not None and resolved_type not in ANOMALY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown anomaly_type '{resolved_type}'. Valid: {ANOMALY_TYPES}",
        )

    # SensorReading rows are tagged via source = "synthetic:<type>" or
    # "synthetic:context" (ADR-010) -- NOT anomaly_type, which real detection
    # will persist too once Task 3 lands (UPPER_CASE labels, see
    # fault_classification.py). Live
    # demo rows carry source='live' and are cleaned by DELETE /upload/live-data
    # instead; the "synthetic:%" predicate already excludes them. Rows injected
    # before this marker existed (source IS NULL) are unreachable here -- see
    # migration 025 and test_should_not_delete_a_legacy_unbackfilled_synthetic_row.
    sensor_where = [
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
        SensorReading.source.like(f"{SYNTHETIC_SOURCE_PREFIX}%"),
    ]
    if resolved_type:
        sensor_where[-1] = SensorReading.source == synthetic_source(resolved_type)

    # RawReading rows are tagged via source = "synthetic:<type>" or "synthetic:context"
    raw_where = [
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
        RawReading.source.like(f"{SYNTHETIC_SOURCE_PREFIX}%"),
    ]
    if resolved_type:
        raw_where[-1] = RawReading.source == synthetic_source(resolved_type)

    sensor_result = await db.execute(delete(SensorReading).where(*sensor_where))
    raw_result = await db.execute(delete(RawReading).where(*raw_where))
    await db.commit()
    await invalidate_response_cache("remove_synthetic_anomalies")

    return {
        "deleted_sensor": sensor_result.rowcount,
        "deleted_raw": raw_result.rowcount,
        "anomaly_type": resolved_type or "all",
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
    }

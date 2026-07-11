"""Read endpoints backing the dashboard (C3: dashboard reads go through the API)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi_cache.decorator import cache

from src.api.query_params import parse_iso_param
from src.api.services import readings_service
from src.api.services.anomaly_insights import filter_anomaly_rows
from src.api.services.downsample import downsample_rows, strip_scores
from src.config import (
    DOWNSAMPLE_MAX_POINTS_CEIL,
    DOWNSAMPLE_MAX_POINTS_FLOOR,
    READINGS_RECENT_HOURS,
    REDIS_CACHE_EXPIRE_SECONDS,
)
from src.db.session import get_db

router = APIRouter(prefix="/api/readings", tags=["Readings"])


def _shape_rows(
    rows: list[dict[str, Any]],
    max_points: Optional[int],
    include_scores: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply G1 downsampling/score-stripping; extras are {} when a no-op.

    Additive contract: with max_points unset and include_scores=True the
    response is byte-identical to the pre-G1 behavior.
    """
    extras: dict[str, Any] = {}
    if max_points is not None:
        original_count = len(rows)
        rows, downsampled = downsample_rows(rows, max_points)
        if downsampled:
            extras = {"downsampled": True, "original_count": original_count}
    if not include_scores:
        rows = strip_scores(rows)
    return rows, extras


_parse_iso = parse_iso_param


@router.get("/window")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_readings_window(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    start: str = Query(...),
    end: str = Query(...),
    limit: Optional[int] = Query(default=None, ge=1),
    max_points: Optional[int] = Query(
        default=None, ge=DOWNSAMPLE_MAX_POINTS_FLOOR, le=DOWNSAMPLE_MAX_POINTS_CEIL
    ),
    include_scores: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Merged raw+canonical rows for an inclusive window (hard row cap applies).

    G1: `max_points` LTTB-downsamples the rows (anomaly rows preserved);
    `include_scores=false` trims anomaly_score from the payload.
    """
    rows = await readings_service.fetch_merged_readings(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=_parse_iso(start, "start"),
        end=_parse_iso(end, "end"),
        limit=limit,
    )
    rows, extras = _shape_rows(rows, max_points, include_scores)
    return {"rows": rows, "count": len(rows), **extras}


@router.get("/recent")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_readings_recent(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    hours: int = Query(default=READINGS_RECENT_HOURS, ge=1),
    limit: Optional[int] = Query(default=None, ge=1),
    max_points: Optional[int] = Query(
        default=None, ge=DOWNSAMPLE_MAX_POINTS_FLOOR, le=DOWNSAMPLE_MAX_POINTS_CEIL
    ),
    include_scores: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Bounded landing-page load: the most recent `hours` of merged readings.

    Anchored to the newest stored timestamp (not wall-clock) so historical
    datasets still render on first load.
    """
    min_iso, max_iso = await readings_service.fetch_timestamp_bounds(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id
    )
    if max_iso is None:
        return {"rows": [], "count": 0, "min_timestamp": None, "max_timestamp": None}
    end = datetime.fromisoformat(max_iso)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(hours=hours)
    rows = await readings_service.fetch_merged_readings(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=start,
        end=end,
        limit=limit,
    )
    rows, extras = _shape_rows(rows, max_points, include_scores)
    return {
        "rows": rows,
        "count": len(rows),
        "min_timestamp": min_iso,
        "max_timestamp": max_iso,
        **extras,
    }


@router.get("/anomalies-in-window")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_anomalies_in_window(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    start: str = Query(...),
    end: str = Query(...),
    threshold: float = Query(..., ge=0.0, le=1.0),
    max_points: Optional[int] = Query(
        default=None, ge=DOWNSAMPLE_MAX_POINTS_FLOOR, le=DOWNSAMPLE_MAX_POINTS_CEIL
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Threshold-filtered anomaly rows for a window (migration plan G2).

    Moves the dashboard's `load_anomalies_for_window` merge+filter
    server-side: merged raw+canonical rows are score-filtered, and
    `max_points` LTTB-downsamples the flagged rows. `total_count` is the
    full window's row count, `anomaly_count` the flagged count before
    downsampling.
    """
    window_rows = await readings_service.fetch_merged_readings(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=_parse_iso(start, "start"),
        end=_parse_iso(end, "end"),
    )
    anomaly_rows = filter_anomaly_rows(window_rows, threshold)
    anomaly_count = len(anomaly_rows)
    anomaly_rows, extras = _shape_rows(anomaly_rows, max_points, include_scores=True)
    return {
        "rows": anomaly_rows,
        "anomaly_count": anomaly_count,
        "total_count": len(window_rows),
        **extras,
    }


@router.get("/bounds")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_readings_bounds(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    min_iso, max_iso = await readings_service.fetch_timestamp_bounds(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id
    )
    return {"min_timestamp": min_iso, "max_timestamp": max_iso}


@router.get("/anomalies")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_anomaly_readings(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    hours_back: int = Query(..., ge=1),
    limit: Optional[int] = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Canonical (scored) rows within the trailing wall-clock window."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_back)
    rows = await readings_service.fetch_canonical_window(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=start,
        end=end,
        limit=limit,
    )
    return {"rows": rows, "count": len(rows)}


@router.get("/raw")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_raw_readings(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    hours_back: int = Query(..., ge=1),
    limit: Optional[int] = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Full-fidelity raw rows (all CSV columns) within the trailing window."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours_back)
    rows = await readings_service.fetch_raw_window(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=start,
        end=end,
        limit=limit,
    )
    return {"rows": rows, "count": len(rows)}


@router.get("/raw/exists")
async def get_raw_exists(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    hours_back: Optional[int] = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    start = None
    end = None
    if hours_back is not None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours_back)
    exists = await readings_service.raw_readings_exist(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id, start=start, end=end
    )
    return {"exists": exists}


@router.get("/greenhouses")
async def get_greenhouses(
    tenant_id: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[str]]:
    return {
        "greenhouses": await readings_service.list_greenhouses(db, tenant_id=tenant_id)
    }


@router.get("/incidents")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_incidents(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    rows = await readings_service.fetch_incidents(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=_parse_iso(start, "start") if start else None,
        end=_parse_iso(end, "end") if end else None,
        limit=limit,
    )
    return {"rows": rows, "count": len(rows)}

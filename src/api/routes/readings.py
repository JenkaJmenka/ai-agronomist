"""Read endpoints backing the dashboard (C3: dashboard reads go through the API)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services import readings_service
from src.config import READINGS_RECENT_HOURS
from src.db.session import get_db

router = APIRouter(prefix="/api/readings", tags=["Readings"])


def _parse_iso(value: str, param: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid ISO timestamp for {param!r}: {value!r}"
        ) from exc


@router.get("/window")
async def get_readings_window(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    start: str = Query(...),
    end: str = Query(...),
    limit: Optional[int] = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Merged raw+canonical rows for an inclusive window (hard row cap applies)."""
    rows = await readings_service.fetch_merged_readings(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=_parse_iso(start, "start"),
        end=_parse_iso(end, "end"),
        limit=limit,
    )
    return {"rows": rows, "count": len(rows)}


@router.get("/recent")
async def get_readings_recent(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    hours: int = Query(default=READINGS_RECENT_HOURS, ge=1),
    limit: Optional[int] = Query(default=None, ge=1),
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
    return {
        "rows": rows,
        "count": len(rows),
        "min_timestamp": min_iso,
        "max_timestamp": max_iso,
    }


@router.get("/bounds")
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

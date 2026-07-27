"""Aggregated anomaly endpoints for the Next.js FE (migration plan G3/G4)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi_cache.decorator import cache
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.query_params import parse_iso_param
from src.api.services import readings_service
from src.api.services.anomaly_group_service import fetch_anomaly_groups
from src.api.services.anomaly_insights import enrich_anomaly_rows
from src.config import (
    ANOMALY_ENRICHED_DEFAULT_LIMIT,
    ANOMALY_ENRICHED_MAX_LIMIT,
    ANOMALY_GROUP_SAMPLE_IDS,
    REDIS_CACHE_EXPIRE_SECONDS,
)
from src.db.session import get_db

router = APIRouter(prefix="/api/anomalies", tags=["Anomalies"])


_parse_iso = parse_iso_param


async def _load_incidents(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    return await readings_service.fetch_incidents(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=_parse_iso(start, "start"),
        end=_parse_iso(end, "end"),
    )


@router.get("/groups")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_anomaly_groups(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    start: str = Query(...),
    end: str = Query(...),
    threshold: float = Query(..., ge=0.0, le=1.0),
    group_by: Literal["date", "type", "sensor"] = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Aggregated anomaly groups (G3) — powers the FE Table view grouping.

    ``sample_ids`` are ISO timestamps of each group's top-scoring rows (rows
    have no stable primary key); ``type``/``sensor`` grouping runs the same
    incident-join enrichment as /enriched so fault labels agree everywhere.

    Performance: uses SQL aggregation (by=date) or column-projection + binary-
    search incident join (by=type/sensor) instead of fetching up to 50 000 full
    rows and processing them in Python.  Full-range full-year queries that
    previously took 33 s now complete in < 1 s.
    """
    scope = {"tenant_id": tenant_id, "greenhouse_id": greenhouse_id}
    window = {"start": _parse_iso(start, "start"), "end": _parse_iso(end, "end")}

    # Fetch incidents once — shared by grouping and anomaly_count.
    incidents = await _load_incidents(db, **scope, start=start, end=end)

    # Exact flagged row count (DISTINCT timestamps, never capped).
    anomaly_count = await readings_service.count_flagged_rows(
        db, **scope, **window, threshold=threshold
    )

    groups = await fetch_anomaly_groups(
        db,
        **scope,
        **window,
        threshold=threshold,
        group_by=group_by,
        sample_limit=ANOMALY_GROUP_SAMPLE_IDS,
        incidents=incidents,
    )

    return {"groups": groups, "group_by": group_by, "anomaly_count": anomaly_count}


@router.get("/enriched")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_enriched_anomalies(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    start: str = Query(...),
    end: str = Query(...),
    threshold: float = Query(..., ge=0.0, le=1.0),
    limit: Optional[int] = Query(
        default=None, ge=1, le=ANOMALY_ENRICHED_MAX_LIMIT
    ),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Enriched top anomalies (G4) — replaces ``load_enriched_top_anomalies``.

    Highest-scoring flagged rows with fault_type / root_cause / reasoning
    from the incident join, ready for the FE Top Anomalies table.

    Performance: uses ORDER BY anomaly_score DESC LIMIT N in SQL so only the
    requested rows are fetched (previously fetched all flagged rows then
    discarded all but 50 in Python — 7.5 s for a full year).
    """
    scope = {"tenant_id": tenant_id, "greenhouse_id": greenhouse_id}
    window = {"start": _parse_iso(start, "start"), "end": _parse_iso(end, "end")}
    effective_limit = limit or ANOMALY_ENRICHED_DEFAULT_LIMIT

    # Use bounded SQL fetch (ORDER BY score DESC LIMIT N) instead of full scan.
    ranked = await readings_service.fetch_top_flagged_rows_by_score(
        db,
        **scope,
        **window,
        threshold=threshold,
        limit=effective_limit,
    )

    # anomaly_count is the full count, not capped by the display limit.
    anomaly_count = await readings_service.count_flagged_rows(
        db, **scope, **window, threshold=threshold
    )

    incidents = await _load_incidents(db, **scope, start=start, end=end)
    rows = enrich_anomaly_rows(ranked, incidents)

    return {"rows": rows, "count": len(rows), "anomaly_count": anomaly_count}

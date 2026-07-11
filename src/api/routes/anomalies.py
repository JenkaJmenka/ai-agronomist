"""Aggregated anomaly endpoints for the Next.js FE (migration plan G3/G4)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi_cache.decorator import cache
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.query_params import parse_iso_param
from src.api.services import readings_service
from src.api.services.anomaly_insights import (
    enrich_anomaly_rows,
    filter_anomaly_rows,
    group_anomaly_rows,
    top_anomalies,
)
from src.config import (
    ANOMALY_ENRICHED_DEFAULT_LIMIT,
    ANOMALY_ENRICHED_MAX_LIMIT,
    ANOMALY_GROUP_SAMPLE_IDS,
    REDIS_CACHE_EXPIRE_SECONDS,
)
from src.db.session import get_db

router = APIRouter(prefix="/api/anomalies", tags=["Anomalies"])


_parse_iso = parse_iso_param


async def _load_flagged_window(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start: str,
    end: str,
    threshold: float,
) -> list[dict[str, Any]]:
    window_rows = await readings_service.fetch_merged_readings(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=_parse_iso(start, "start"),
        end=_parse_iso(end, "end"),
    )
    return filter_anomaly_rows(window_rows, threshold)


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

    `sample_ids` are ISO timestamps of each group's top-scoring rows (rows
    have no stable primary key); `type`/`sensor` grouping runs the same
    incident-join enrichment as /enriched so fault labels agree everywhere.
    """
    flagged = await _load_flagged_window(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=start,
        end=end,
        threshold=threshold,
    )
    if group_by in ("type", "sensor"):
        incidents = await _load_incidents(
            db, tenant_id=tenant_id, greenhouse_id=greenhouse_id, start=start, end=end
        )
        flagged = enrich_anomaly_rows(flagged, incidents)
    groups = group_anomaly_rows(
        flagged, group_by, sample_limit=ANOMALY_GROUP_SAMPLE_IDS
    )
    return {"groups": groups, "group_by": group_by, "anomaly_count": len(flagged)}


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
    """Enriched top anomalies (G4) — replaces `load_enriched_top_anomalies`.

    Highest-scoring flagged rows with fault_type / root_cause / reasoning
    from the incident join, ready for the FE Top Anomalies table.
    """
    flagged = await _load_flagged_window(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start=start,
        end=end,
        threshold=threshold,
    )
    ranked = top_anomalies(flagged, limit or ANOMALY_ENRICHED_DEFAULT_LIMIT)
    incidents = await _load_incidents(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id, start=start, end=end
    )
    rows = enrich_anomaly_rows(ranked, incidents)
    return {"rows": rows, "count": len(rows), "anomaly_count": len(flagged)}

"""Aggregated anomaly endpoints for the Next.js FE (migration plan G3/G4)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi_cache.decorator import cache
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.query_params import parse_iso_param
from src.api.schemas.anomaly_episodes import AnomalyEpisodesResponse
from src.api.services import readings_service
from src.api.services.anomaly_episode_service import fetch_anomaly_episodes
from src.api.services.anomaly_group_service import (
    _build_sorted_incidents,
    _enrich_samples_fast,
    fetch_anomaly_groups,
)
from src.config import (
    ANOMALY_ENRICHED_DEFAULT_LIMIT,
    ANOMALY_ENRICHED_MAX_LIMIT,
    ANOMALY_GROUP_SAMPLE_IDS,
    CANONICAL_ANOMALY_THRESHOLD,
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
    threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    group_by: Literal["date", "type", "sensor"] = Query(...),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> dict[str, Any]:
    """Aggregated anomaly groups (G3) — powers the FE Table view grouping.

    ``sample_ids`` are ISO timestamps of each group's top-scoring rows (rows
    have no stable primary key); ``type``/``sensor`` grouping runs the same
    incident-join enrichment as /enriched so fault labels agree everywhere.

    Performance: uses SQL aggregation (by=date) or column-projection + binary-
    search incident join (by=type/sensor) instead of fetching up to 50 000 full
    rows and processing them in Python.  Full-range full-year queries that
    previously took 33 s now complete in < 1 s.

    ADR-005 §2 — threshold is now optional.  When omitted the server defaults to
    ``CANONICAL_ANOMALY_THRESHOLD = 0.5``, which is correct for every producer on
    the canonical scale.  The effective value is echoed in the response so clients
    can display exactly which threshold was applied.

    Cache key note: the cache is keyed on all query parameters.  An absent
    ``threshold`` and an explicit ``?threshold=0.5`` produce different cache keys
    and are cached independently; both return the same result and both are correct.
    A stale ``?threshold=0.15`` entry from before the migration will age out
    within REDIS_CACHE_EXPIRE_SECONDS.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    effective_threshold = threshold if threshold is not None else CANONICAL_ANOMALY_THRESHOLD
    scope = {"tenant_id": tenant_id, "greenhouse_id": greenhouse_id}
    window = {"start": _parse_iso(start, "start"), "end": _parse_iso(end, "end")}

    # Fetch incidents once — shared by grouping and anomaly_count.
    incidents = await _load_incidents(db, **scope, start=start, end=end)

    # Exact flagged row count (DISTINCT timestamps, never capped).
    anomaly_count = await readings_service.count_flagged_rows(
        db, **scope, **window, threshold=effective_threshold
    )

    groups = await fetch_anomaly_groups(
        db,
        **scope,
        **window,
        threshold=effective_threshold,
        group_by=group_by,
        sample_limit=ANOMALY_GROUP_SAMPLE_IDS,
        incidents=incidents,
    )

    return {
        "groups": groups,
        "group_by": group_by,
        "anomaly_count": anomaly_count,
        "threshold": effective_threshold,
        "score_scale_version": 2,
    }


@router.get("/episodes", response_model=AnomalyEpisodesResponse)
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_anomaly_episodes(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    start: str = Query(...),
    end: str = Query(...),
    threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> dict[str, Any]:
    """Query-time anomaly episode merging — one row per continuous event.

    Merges contiguous 5-minute flagged rows sharing a (channel, detector,
    direction) key into episodes with start/end/duration/trend, reusing the
    same contiguity rule as the upload-path detector
    (``src/models/episode_grouper.py``) so the two views never disagree.  See
    ``docs/architecture/episode_grouping.md`` and
    ``src/api/services/anomaly_episode_service.py`` for the grouping rules.

    Episodes are built from EVERY flagged row in the window
    (``_fetch_minimal_flagged_rows``), never from a top-N-by-score subset —
    grouping a top-N list would manufacture gaps that do not exist in the
    data.  Episodes are ranked by ``peak_score`` and capped at
    ``ANOMALY_EPISODE_MAX`` only after grouping.

    ADR-005 §2 — threshold is optional, same handling as ``/groups``: the
    server defaults to ``CANONICAL_ANOMALY_THRESHOLD`` and echoes the
    effective value in the response.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    effective_threshold = threshold if threshold is not None else CANONICAL_ANOMALY_THRESHOLD
    scope = {"tenant_id": tenant_id, "greenhouse_id": greenhouse_id}
    window = {"start": _parse_iso(start, "start"), "end": _parse_iso(end, "end")}

    # Fetch incidents once — shared by episode enrichment and anomaly_count.
    incidents = await _load_incidents(db, **scope, start=start, end=end)

    # Exact flagged row count (DISTINCT timestamps, never capped).
    anomaly_count = await readings_service.count_flagged_rows(
        db, **scope, **window, threshold=effective_threshold
    )

    episodes = await fetch_anomaly_episodes(
        db,
        **scope,
        **window,
        threshold=effective_threshold,
        sample_limit=ANOMALY_GROUP_SAMPLE_IDS,
        incidents=incidents,
    )

    return {
        "episodes": episodes,
        "episode_count": len(episodes),
        "anomaly_count": anomaly_count,
        "threshold": effective_threshold,
        "score_scale_version": 2,
    }


@router.get("/enriched")
@cache(expire=REDIS_CACHE_EXPIRE_SECONDS)
async def get_enriched_anomalies(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    start: str = Query(...),
    end: str = Query(...),
    threshold: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    limit: Optional[int] = Query(
        default=None, ge=1, le=ANOMALY_ENRICHED_MAX_LIMIT
    ),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> dict[str, Any]:
    """Enriched top anomalies (G4) — replaces ``load_enriched_top_anomalies``.

    Highest-scoring flagged rows with fault_type / root_cause / reasoning
    from the incident join, ready for the FE Top Anomalies table.

    Performance (two-part fix shipped 2026-07-28):

    1. Migration 022 adds ``ix_sensor_readings_score_desc`` on
       ``(tenant_id, greenhouse_id, anomaly_score DESC) WHERE anomaly_score IS NOT NULL``.
       This lets Postgres walk the index from the highest score downward and stop
       after LIMIT rows, avoiding the full-table seq scan + top-N heapsort that
       dominated at FULL RANGE.

       EXPLAIN ANALYZE before (35k rows, Postgres 16):
         Seq Scan + top-N heapsort over 4 381 filtered rows — 10.570 ms local,
         ~9.8 s in production.

       EXPLAIN ANALYZE after:
         Index Scan on ix_sensor_readings_score_desc — 0.277 ms local (38x).

    2. The incident-join enrichment switches from ``enrich_anomaly_rows``
       (pandas DataFrame + O(N_incidents) linear scan per row) to
       ``_enrich_samples_fast`` (binary-search incident lookup, no pandas),
       reusing the same fast path already in use by /groups.

    Note: the three sequential awaits (fetch_top_flagged_rows_by_score,
    count_flagged_rows, _load_incidents) are independent but CANNOT be
    parallelised with asyncio.gather: AsyncSession is backed by a single
    connection and does not support concurrent queries on the same session.
    Running them in parallel would raise sqlalchemy.exc.InvalidRequestError.

    Response shape is preserved exactly: rows, count, anomaly_count.
    anomaly_count is the full window count — never capped by the display limit.

    ADR-005 §2 — threshold is now optional.  Echoes the effective threshold and
    score_scale_version so clients can confirm which scale is in use.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    effective_threshold = threshold if threshold is not None else CANONICAL_ANOMALY_THRESHOLD
    scope = {"tenant_id": tenant_id, "greenhouse_id": greenhouse_id}
    window = {"start": _parse_iso(start, "start"), "end": _parse_iso(end, "end")}
    effective_limit = limit or ANOMALY_ENRICHED_DEFAULT_LIMIT

    # Bounded SQL fetch — ix_sensor_readings_score_desc makes this an index walk
    # that stops at LIMIT rows without sorting the entire filtered set.
    ranked = await readings_service.fetch_top_flagged_rows_by_score(
        db,
        **scope,
        **window,
        threshold=effective_threshold,
        limit=effective_limit,
    )

    # Full window count — must stay uncapped regardless of the display limit.
    anomaly_count = await readings_service.count_flagged_rows(
        db, **scope, **window, threshold=effective_threshold
    )

    # Load incidents for the window then build a binary-search index so each
    # of the top-N rows can find its nearest incident in O(log N_incidents).
    # The fast path avoids pandas DataFrame construction and the O(N_incidents)
    # linear scan that enrich_anomaly_rows ran for every row.
    incidents = await _load_incidents(db, **scope, start=start, end=end)
    sorted_times, sorted_incidents = _build_sorted_incidents(incidents)
    rows = _enrich_samples_fast(ranked, sorted_times, sorted_incidents)

    return {
        "rows": rows,
        "count": len(rows),
        "anomaly_count": anomaly_count,
        "threshold": effective_threshold,
        "score_scale_version": 2,
    }

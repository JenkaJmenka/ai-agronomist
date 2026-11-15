"""
Read-only API endpoints for the drift-watch monitor subsystem.

Exposes the content of ``drift_watch_inputs`` and ``drift_watch_episodes``
(populated by ``run_drift_watch_shadow`` at the end of each CSV upload) so
the operator UI can present drift status and episode history without
touching the raw spec/calibration/state JSON blobs.

Authentication and tenant scoping follow the same pattern as the other
tenant-scoped routes: ``require_api_key`` resolves the caller's effective
tenant id, and ``enforce_tenant_access`` checks every query against it so
a caller can never read another tenant's rows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.config import DRIFT_WATCH_MIN_HISTORY_WEEKS, DRIFT_WATCH_QUIET_HOURS
from src.db.models import DriftWatchEpisode, DriftWatchInput
from src.db.session import get_db
from src.api.schemas.drift_watch import DriftWatchEpisodeResponse, DriftWatchInputResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/drift-watch", tags=["drift-watch"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _aware(dt: datetime) -> datetime:
    """Return *dt* with UTC attached when it has no tzinfo (SQLite rows)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _input_to_response(row: DriftWatchInput) -> DriftWatchInputResponse:
    """Build a ``DriftWatchInputResponse`` from an ORM row."""
    spec: dict = json.loads(row.spec_json)
    kind: str = spec.get("kind", "")
    target_channel: str = spec.get("target_channel", "")

    # Compute status_reason from the calibration result when present.
    status_reason: str
    cal: Optional[dict] = None
    if row.calibration_json:
        cal = json.loads(row.calibration_json)

    if row.status == "uncalibrated":
        status_reason = f"awaiting {DRIFT_WATCH_MIN_HISTORY_WEEKS} weeks of history"
    elif row.status == "derived":
        status_reason = (
            "humidity deficit is derived from temperature and RH on this site"
        )
    elif row.status == "shadow" and cal is not None:
        status_reason = (
            cal.get("shadow_reason")
            or "shadow mode (calibration constraints not met)"
        )
    else:
        status_reason = ""

    # Calibration summary fields — present only when calibration has run.
    false_alarm_rate: Optional[float] = None
    detection_rate: Optional[float] = None
    if cal is not None:
        false_alarm_rate = cal.get("measured_fa_per_month")
        self_test: list = cal.get("self_test") or []
        rates = [
            t["detection_rate"]
            for t in self_test
            if isinstance(t.get("detection_rate"), (int, float))
        ]
        if rates:
            detection_rate = min(rates)

    # calibrated_at: use updated_at as a proxy when calibration_json exists;
    # None for uncalibrated inputs where it has never been set.
    calibrated_at: Optional[datetime] = row.updated_at if cal is not None else None

    return DriftWatchInputResponse(
        input_id=row.input_id,
        kind=kind,
        target_channel=target_channel,
        status=row.status,
        status_reason=status_reason,
        calibrated_at=calibrated_at,
        updated_at=row.updated_at,
        false_alarm_rate=false_alarm_rate,
        detection_rate=detection_rate,
    )


def _episode_to_response(
    ep: DriftWatchEpisode,
    input_updated_at: datetime,
) -> DriftWatchEpisodeResponse:
    """Build a ``DriftWatchEpisodeResponse`` from an ORM row.

    ``input_updated_at`` is the ``updated_at`` of the parent
    ``DriftWatchInput`` row (queried via JOIN).  ``ongoing`` is ``True``
    when the episode's ``last_confirmed_ts`` is within
    ``DRIFT_WATCH_QUIET_HOURS`` of that timestamp, meaning the drift was
    still present in the most recently processed upload.
    """
    last_confirmed = _aware(ep.last_confirmed_ts)
    upd_at = _aware(input_updated_at)
    delta_hours = abs((upd_at - last_confirmed).total_seconds()) / 3600.0
    ongoing = delta_hours <= DRIFT_WATCH_QUIET_HOURS

    return DriftWatchEpisodeResponse(
        input_id=ep.input_id,
        episode_id=ep.episode_id,
        target_channel=ep.target_channel,
        direction=ep.direction,
        first_detected_ts=ep.first_detected_ts,
        last_confirmed_ts=ep.last_confirmed_ts,
        onset_estimate_ts=ep.onset_estimate_ts,
        offset_estimate=ep.offset_estimate,
        peak_statistic=ep.peak_statistic,
        input_status_at_detection=ep.input_status_at_detection,
        ongoing=ongoing,
        armed=ep.input_status_at_detection == "armed",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/inputs", response_model=List[DriftWatchInputResponse])
async def list_drift_watch_inputs(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> List[DriftWatchInputResponse]:
    """List drift-watch inputs (monitor specs) for the caller's tenant.

    Returns one entry per ``(tenant_id, greenhouse_id, input_id)`` triple,
    exposing the calibration status and summary metrics without the raw JSON
    blobs.
    """
    enforce_tenant_access(auth_tenant, tenant_id)

    stmt = select(DriftWatchInput).where(DriftWatchInput.tenant_id == tenant_id)
    if greenhouse_id is not None:
        stmt = stmt.where(DriftWatchInput.greenhouse_id == greenhouse_id)

    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_input_to_response(row) for row in rows]


@router.get("/episodes", response_model=List[DriftWatchEpisodeResponse])
async def list_drift_watch_episodes(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    until: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> List[DriftWatchEpisodeResponse]:
    """List drift episodes for the caller's tenant, newest first.

    The ``ongoing`` flag in each episode is computed by comparing
    ``last_confirmed_ts`` against the parent input's ``updated_at``:
    an episode is ongoing when the gap is at most ``DRIFT_WATCH_QUIET_HOURS``
    hours, meaning the drift was confirmed in the most recent upload batch.

    ``since`` / ``until`` filter on ``first_detected_ts``.
    """
    enforce_tenant_access(auth_tenant, tenant_id)

    stmt = (
        select(DriftWatchEpisode, DriftWatchInput.updated_at)
        .join(
            DriftWatchInput,
            and_(
                DriftWatchEpisode.tenant_id == DriftWatchInput.tenant_id,
                DriftWatchEpisode.greenhouse_id == DriftWatchInput.greenhouse_id,
                DriftWatchEpisode.input_id == DriftWatchInput.input_id,
            ),
        )
        .where(DriftWatchEpisode.tenant_id == tenant_id)
    )
    if greenhouse_id is not None:
        stmt = stmt.where(DriftWatchEpisode.greenhouse_id == greenhouse_id)
    if since is not None:
        stmt = stmt.where(DriftWatchEpisode.first_detected_ts >= since)
    if until is not None:
        stmt = stmt.where(DriftWatchEpisode.first_detected_ts <= until)

    stmt = stmt.order_by(DriftWatchEpisode.first_detected_ts.desc()).limit(limit)

    result = await db.execute(stmt)
    rows = result.all()
    return [_episode_to_response(ep, input_updated_at) for ep, input_updated_at in rows]

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
import math
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import (
    ADMIN_SCOPE,
    CallerIdentity,
    enforce_tenant_access,
    require_api_key,
    require_scope,
)
from src.api.services.drift_watch_alerts import (
    AlertEnableError,
    set_alert_state_manually,
    build_drift_alert_message,
    oos_counts_for_input,
)
from src.api.services.drift_watch_service import _spec_columns, load_hourly_frame
from src.config import (
    DRIFT_WATCH_ALERT_ENABLE_MAX_COUNT,
    DRIFT_WATCH_ALERT_MIN_OFFSET_MULT,
    DRIFT_WATCH_BASELINE_WEEKS,
    DRIFT_WATCH_MIN_HISTORY_WEEKS,
    DRIFT_WATCH_QUIET_HOURS,
)
from src.db.models import DriftWatchEpisode, DriftWatchFeedback, DriftWatchInput
from src.models.drift_watch import (
    CalibrationResult,
    DriftInputSpec,
    InputKind,
    replay_series,
)
from src.db.session import get_db
from src.api.schemas.drift_watch import (
    DriftWatchEpisodeResponse,
    DriftWatchFeedbackRequest,
    DriftWatchFeedbackResponse,
    DriftWatchInputResponse,
    DriftWatchAlertStateResponse,
    DriftSeriesEpisode,
    DriftSeriesPoint,
    DriftWatchSeriesResponse,
)

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


def _input_to_response(
    row: DriftWatchInput,
    oos_count: Optional[int] = None,
    raw_count: Optional[int] = None,
) -> DriftWatchInputResponse:
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
        greenhouse_id=row.greenhouse_id,
        alert_state=row.alert_state,
        alert_state_reason=row.alert_state_reason,
        oos_count=oos_count,
        raw_count=raw_count,
        budget=DRIFT_WATCH_ALERT_ENABLE_MAX_COUNT,
    )


def _episode_to_response(
    ep: DriftWatchEpisode,
    input_updated_at: datetime,
    spec_json: Optional[str] = None,
    label: Optional[str] = None,
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

    alert_message: Optional[str] = None
    if spec_json is not None and ep.offset_robust is not None:
        try:
            sd = json.loads(spec_json)
            alert_message = build_drift_alert_message(
                DriftInputSpec(
                    input_id=sd["input_id"],
                    kind=InputKind(sd["kind"]),
                    target_channel=sd["target_channel"],
                    channels=list(sd["channels"]),
                    min_shift=float(sd["min_shift"]),
                ),
                ep.direction,
                ep.offset_robust,
            )
        except (KeyError, ValueError, TypeError):
            alert_message = None

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
        offset_robust=ep.offset_robust,
        suppressed_by=ep.suppressed_by,
        label=label,
        alert_message=alert_message,
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
    out: List[DriftWatchInputResponse] = []
    for row in rows:
        count, raw = await oos_counts_for_input(db, row)
        out.append(_input_to_response(row, count, raw))
    return out


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
        select(DriftWatchEpisode, DriftWatchInput.updated_at, DriftWatchInput.spec_json)
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

    fb_stmt = select(DriftWatchFeedback).where(DriftWatchFeedback.tenant_id == tenant_id)
    if greenhouse_id is not None:
        fb_stmt = fb_stmt.where(DriftWatchFeedback.greenhouse_id == greenhouse_id)
    labels = {
        (f.greenhouse_id, f.input_id, f.direction, _aware(f.first_detected_ts)): f.label
        for f in (await db.execute(fb_stmt)).scalars().all()
    }
    return [
        _episode_to_response(
            ep,
            input_updated_at,
            spec_json,
            labels.get(
                (ep.greenhouse_id, ep.input_id, ep.direction, _aware(ep.first_detected_ts))
            ),
        )
        for ep, input_updated_at, spec_json in rows
    ]


@router.post("/feedback", response_model=DriftWatchFeedbackResponse)
async def post_drift_watch_feedback(
    body: DriftWatchFeedbackRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> DriftWatchFeedbackResponse:
    """Label a drift episode ``confirmed_drift`` or ``false_alarm`` (upsert).

    The episode is addressed by its natural key (ADR-013 §4), not
    ``episode_id``, so the label survives a backfill. 404 when no such episode
    exists in the caller's tenant.
    """
    enforce_tenant_access(auth_tenant, body.tenant_id)
    first_ts = _aware(body.first_detected_ts).astimezone(timezone.utc)

    ep = (
        await db.execute(
            select(DriftWatchEpisode).where(
                DriftWatchEpisode.tenant_id == body.tenant_id,
                DriftWatchEpisode.greenhouse_id == body.greenhouse_id,
                DriftWatchEpisode.input_id == body.input_id,
                DriftWatchEpisode.direction == body.direction,
                DriftWatchEpisode.first_detected_ts == first_ts,
            )
        )
    ).scalars().first()
    if ep is None:
        raise HTTPException(status_code=404, detail="Drift episode not found")

    row = (
        await db.execute(
            select(DriftWatchFeedback).where(
                DriftWatchFeedback.tenant_id == body.tenant_id,
                DriftWatchFeedback.greenhouse_id == body.greenhouse_id,
                DriftWatchFeedback.input_id == body.input_id,
                DriftWatchFeedback.direction == body.direction,
                DriftWatchFeedback.first_detected_ts == first_ts,
            )
        )
    ).scalars().first()
    if row is None:
        row = DriftWatchFeedback(
            tenant_id=body.tenant_id,
            greenhouse_id=body.greenhouse_id,
            input_id=body.input_id,
            direction=body.direction,
            first_detected_ts=first_ts,
            label=body.label,
            note=body.note,
            created_by=body.created_by,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
    else:
        row.label = body.label
        row.note = body.note
        row.created_by = body.created_by
    await db.commit()
    await db.refresh(row)
    return DriftWatchFeedbackResponse.model_validate(row)


async def _set_alert_state(
    *,
    enable: bool,
    tenant_id: str,
    greenhouse_id: str,
    input_id: str,
    db: AsyncSession,
    auth_tenant: str | None,
    caller: Optional[CallerIdentity],
) -> DriftWatchAlertStateResponse:
    """Shared body of the admin enable/disable endpoints."""
    enforce_tenant_access(auth_tenant, tenant_id)
    row = (
        await db.execute(
            select(DriftWatchInput).where(
                DriftWatchInput.tenant_id == tenant_id,
                DriftWatchInput.greenhouse_id == greenhouse_id,
                DriftWatchInput.input_id == input_id,
            )
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Drift-watch input not found")
    previous = row.alert_state
    try:
        await set_alert_state_manually(db, row, enable=enable)
    except AlertEnableError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "drift-watch alert %s: tenant=%s greenhouse=%s input=%s %s -> %s "
        "by credential=%s (%s) at %s",
        "enabled" if enable else "disabled",
        tenant_id,
        greenhouse_id,
        input_id,
        previous,
        row.alert_state,
        caller.key_id if caller else "auth-disabled",
        caller.name if caller else "auth-disabled",
        datetime.now(timezone.utc).isoformat(),
    )
    return DriftWatchAlertStateResponse(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        input_id=input_id,
        alert_state=row.alert_state,
        alert_state_reason=row.alert_state_reason,
    )


@router.post(
    "/inputs/{tenant_id}/{greenhouse_id}/{input_id}/alerts/enable",
    response_model=DriftWatchAlertStateResponse,
)
async def enable_drift_watch_alerts(
    tenant_id: str,
    greenhouse_id: str,
    input_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
    caller: Optional[CallerIdentity] = Depends(require_scope(ADMIN_SCOPE)),
) -> DriftWatchAlertStateResponse:
    """Admin: enable alerts for one input (``eligible -> enabled``).

    Needs the ``admin`` credential scope. 409 (with the reason) when alerts
    are off globally or the input is not ``eligible``; 404 for an unknown input.
    """
    return await _set_alert_state(
        enable=True, tenant_id=tenant_id, greenhouse_id=greenhouse_id,
        input_id=input_id, db=db, auth_tenant=auth_tenant, caller=caller,
    )


@router.post(
    "/inputs/{tenant_id}/{greenhouse_id}/{input_id}/alerts/disable",
    response_model=DriftWatchAlertStateResponse,
)
async def disable_drift_watch_alerts(
    tenant_id: str,
    greenhouse_id: str,
    input_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
    caller: Optional[CallerIdentity] = Depends(require_scope(ADMIN_SCOPE)),
) -> DriftWatchAlertStateResponse:
    """Admin: disable alerts for one input (any state -> ``off``).

    Needs the ``admin`` credential scope. 404 for an unknown input.
    """
    return await _set_alert_state(
        enable=False, tenant_id=tenant_id, greenhouse_id=greenhouse_id,
        input_id=input_id, db=db, auth_tenant=auth_tenant, caller=caller,
    )


# ---------------------------------------------------------------------------
# Series (drift chart)
# ---------------------------------------------------------------------------

_SERIES_DEFAULT_WEEKS = 13
_SERIES_MAX_WEEKS = 53
_ONSET_MATCH_TOLERANCE = timedelta(hours=1)


def _unit_for(spec: DriftInputSpec) -> str:
    if spec.kind == InputKind.PHYSICS:
        return "g/m³"
    if spec.target_channel == "humidity":
        return "%RH"
    return "°C"


def _fin(val: Any) -> Optional[float]:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _replayed_onsets(full: Any) -> List[tuple]:
    """(ts, direction) of each replayed alarm run start, per direction.

    A run ends after ``DRIFT_WATCH_QUIET_HOURS`` without an alarm of that
    direction, the same gap the detector uses to close an episode.
    """
    out: List[tuple] = []
    last: dict = {}
    gap = timedelta(hours=DRIFT_WATCH_QUIET_HOURS)
    for ts, d in zip(full["ts"], full["alarm_dir"]):
        if d not in ("+", "-"):
            continue
        t = ts.to_pydatetime()
        if d not in last or t - last[d] > gap:
            out.append((t, d))
        last[d] = t
    return out


@router.get(
    "/inputs/{tenant_id}/{greenhouse_id}/{input_id}/series",
    response_model=DriftWatchSeriesResponse,
)
async def get_drift_watch_series(
    tenant_id: str,
    greenhouse_id: str,
    input_id: str,
    start: Optional[datetime] = Query(None),
    end: Optional[datetime] = Query(None),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> DriftWatchSeriesResponse:
    """Replayed drift statistics for one input, for the drift chart.

    Read-only. Replays the immutable calibration (``calibration_json``) cold
    over hourly means built exactly as drift watch builds them (synthetic
    rows excluded). Episode bands come from stored episodes, not from the
    replay; ``replay_mismatch`` counts disagreements between the two.
    Default window: the 13 weeks up to the input's last processed timestamp.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    row = (
        await db.execute(
            select(DriftWatchInput).where(
                DriftWatchInput.tenant_id == tenant_id,
                DriftWatchInput.greenhouse_id == greenhouse_id,
                DriftWatchInput.input_id == input_id,
            )
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Drift-watch input not found")

    sd = json.loads(row.spec_json)
    spec = DriftInputSpec(
        input_id=sd["input_id"],
        kind=InputKind(sd["kind"]),
        target_channel=sd["target_channel"],
        channels=list(sd["channels"]),
        min_shift=float(sd["min_shift"]),
    )
    cal: Optional[CalibrationResult] = None
    if row.calibration_json:
        cal = CalibrationResult.from_dict(json.loads(row.calibration_json))
    last_ts: Optional[datetime] = None
    if row.state_json:
        lp = json.loads(row.state_json).get("last_processed_ts")
        if lp:
            last_ts = _aware(datetime.fromisoformat(lp)).astimezone(timezone.utc)

    win_end = _aware(end).astimezone(timezone.utc) if end else (last_ts or _aware(row.updated_at))
    win_start = (
        _aware(start).astimezone(timezone.utc)
        if start
        else win_end - timedelta(weeks=_SERIES_DEFAULT_WEEKS)
    )
    if win_end <= win_start:
        raise HTTPException(status_code=422, detail="end must be after start")
    if win_end - win_start > timedelta(weeks=_SERIES_MAX_WEEKS):
        raise HTTPException(
            status_code=422,
            detail=f"window exceeds {_SERIES_MAX_WEEKS} weeks",
        )

    cal_end: Optional[datetime] = None
    if cal is not None and cal.calibration_window_end:
        cal_end = _aware(datetime.fromisoformat(cal.calibration_window_end))

    points: List[DriftSeriesPoint] = []
    mismatch = 0
    onsets: List[tuple] = []
    reason: Optional[str] = None
    if cal is None:
        reason = "input has no calibration yet"
    else:
        frame = await load_hourly_frame(
            db, tenant_id, greenhouse_id,
            win_start - timedelta(weeks=DRIFT_WATCH_BASELINE_WEEKS + 2),
            win_end + timedelta(hours=1),
            _spec_columns(spec),
        )
        if frame.empty:
            reason = "no raw readings in the window"
        else:
            full = replay_series(frame, spec, cal)
            win = full[(full["ts"] >= win_start) & (full["ts"] <= win_end)]
            for r in win.itertuples(index=False):
                points.append(
                    DriftSeriesPoint(
                        ts=r.ts.to_pydatetime(),
                        target=_fin(r.target),
                        reference=_fin(r.reference),
                        residual=_fin(r.residual),
                        dev=_fin(r.dev),
                        s_pos=_fin(r.s_pos),
                        s_neg=_fin(r.s_neg),
                        alarm_dir=r.alarm_dir if r.alarm_dir in ("+", "-") else None,
                    )
                )
            if not points:
                reason = "no scorable hours in the window"
            onsets = [
                o for o in _replayed_onsets(full) if win_start <= o[0] <= win_end
            ]

    ep_rows = (
        await db.execute(
            select(DriftWatchEpisode).where(
                DriftWatchEpisode.tenant_id == tenant_id,
                DriftWatchEpisode.greenhouse_id == greenhouse_id,
                DriftWatchEpisode.input_id == input_id,
                DriftWatchEpisode.first_detected_ts >= win_start,
                DriftWatchEpisode.first_detected_ts <= win_end,
            ).order_by(DriftWatchEpisode.first_detected_ts)
        )
    ).scalars().all()
    labels = {
        (f.direction, _aware(f.first_detected_ts)): f.label
        for f in (
            await db.execute(
                select(DriftWatchFeedback).where(
                    DriftWatchFeedback.tenant_id == tenant_id,
                    DriftWatchFeedback.greenhouse_id == greenhouse_id,
                    DriftWatchFeedback.input_id == input_id,
                )
            )
        ).scalars().all()
    }
    episodes = [
        DriftSeriesEpisode(
            first_detected_ts=_aware(e.first_detected_ts),
            onset_ts=_aware(e.onset_estimate_ts) if e.onset_estimate_ts else None,
            last_confirmed_ts=_aware(e.last_confirmed_ts),
            direction=e.direction,
            offset_robust=e.offset_robust,
            label=labels.get((e.direction, _aware(e.first_detected_ts))),
            suppressed_by=e.suppressed_by,
        )
        for e in ep_rows
    ]
    if cal is not None and points:
        # In-sample hours (<= calibration_window_end) were never scored live
        # when the state was first built, so they are not compared.
        stored = [
            (ep.first_detected_ts, ep.direction)
            for ep in episodes
            if cal_end is None or ep.first_detected_ts > cal_end
        ]
        onsets = [o for o in onsets if cal_end is None or o[0] > cal_end]
        tol = _ONSET_MATCH_TOLERANCE
        mismatch = sum(
            1 for ts, d in stored
            if not any(d == od and abs(ts - ot) <= tol for ot, od in onsets)
        ) + sum(
            1 for ot, od in onsets
            if not any(d == od and abs(ts - ot) <= tol for ts, d in stored)
        )

    return DriftWatchSeriesResponse(
        input_id=row.input_id,
        kind=spec.kind.value,
        target_channel=spec.target_channel,
        unit=_unit_for(spec),
        status=row.status,
        alert_state=row.alert_state,
        calibration_window_end=cal_end,
        min_shift=spec.min_shift,
        alert_floor=DRIFT_WATCH_ALERT_MIN_OFFSET_MULT * spec.min_shift,
        h=cal.h if cal else None,
        sigma=cal.scale_sigma if cal else None,
        window_start=win_start,
        window_end=win_end,
        points=points,
        episodes=episodes,
        replay_mismatch=mismatch,
        reason=reason,
    )

"""Multi-table cascade wipes behind the DELETE /upload data-management routes.

Two entry points, matching the two routes in ``src/api/routes/upload.py``:

- ``wipe_tenant_data`` — delete all greenhouse-scoped rows for a
  tenant/greenhouse: SensorReading + RawReading (including synthetic inject
  rows), related Incident / IncidentAction rows, operator feedback, learning
  logs, metrics snapshots, and the live Redis stream.
- ``wipe_live_demo_data`` — Go Live / mock SCADA persisted rows only; client
  CSV uploads and inject-test synthetic rows are left untouched.

Both mute live mock/SCADA publish + DB persistence for the scope first —
otherwise mock SCADA immediately re-seeds the tables that were just wiped.

A wipe also writes ``desired_state='paused'`` to ``live_scope_state``, so the
mute survives a redeploy. Without that, muting is in-process only: the next
API restart would read a stale ``live`` row, resume the scope, and re-seed
the data the operator just cleared — the exact outcome the in-process mute
exists to prevent. **A wipe always wins over a prior Go Live.**
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.cache_invalidation import invalidate_response_cache
from src.api.services.live_scope_state_service import (
    DesiredScopeState,
    store_desired_state,
)
from src.db.models import (
    Incident,
    IncidentAction,
    LearningLog,
    MetricsSnapshot,
    OperatorFeedback,
    RawReading,
    SensorReading,
)

logger = logging.getLogger(__name__)

# source column value for live SCADA rows in both SensorReading and RawReading
# (written by src/live/persistence.py; see migration 017).
LIVE_RAW_SOURCE = "live"


async def _pause_live_scope(
    db: AsyncSession,
    live_runtime: Any,
    tenant_id: str,
    greenhouse_id: str,
    context: str,
) -> bool:
    """Mute the scope in-process AND record ``paused`` as its desired state.

    Must be called **before** the wipe's own DELETEs: a failed desired-state
    upsert rolls the session back, and ordering it first means the rollback
    can only ever discard this one statement rather than the deletes.

    Returns whether the in-process mute took effect; the durable write is
    best-effort and only logged, because a wipe must still clear the data on
    a deploy where migration 028 has not run.
    """
    await store_desired_state(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        desired_state=DesiredScopeState.PAUSED,
    )
    return _suppress_live_persistence(live_runtime, tenant_id, greenhouse_id, context)


def _suppress_live_persistence(
    live_runtime: Any,
    tenant_id: str,
    greenhouse_id: str,
    context: str,
) -> bool:
    """Mute live publish + DB persistence for this scope until Go Live resumes.

    Returns ``True`` when suppression took effect. Never raises — the wipe
    must succeed even if the live runtime is wedged.
    """
    if live_runtime is None:
        from src.live.scope_control import suppress_live_scope

        suppress_live_scope(tenant_id, greenhouse_id)
        return True
    try:
        live_runtime.suppress_persistence(tenant_id, greenhouse_id)
        return True
    except Exception:  # noqa: BLE001 — clear must succeed even if runtime is wedged
        logger.warning(
            "%s: live persistence suppress failed for %s/%s",
            context,
            tenant_id,
            greenhouse_id,
            exc_info=True,
        )
        return False


async def _clear_live_stream(tenant_id: str, greenhouse_id: str, context: str) -> bool:
    """Best-effort: drop the live replay buffer so cleared rows don't reappear
    from Redis XREAD until fresh events are published after Go Live resume."""
    try:
        from src.api.services.live_hub_provider import get_live_hub

        return await get_live_hub().clear_stream(tenant_id, greenhouse_id)
    except Exception:  # noqa: BLE001 — clear must succeed even if Redis is down
        logger.warning(
            "%s: live stream clear failed for %s/%s",
            context,
            tenant_id,
            greenhouse_id,
            exc_info=True,
        )
        return False


async def wipe_tenant_data(
    db: AsyncSession,
    live_runtime: Any,
    tenant_id: str,
    greenhouse_id: str,
) -> dict:
    """Delete all greenhouse-scoped data for a tenant/greenhouse (full wipe)."""
    # Mute live pipeline first so mock SCADA cannot write during/after the wipe.
    live_persistence_suppressed = await _pause_live_scope(
        db, live_runtime, tenant_id, greenhouse_id, "clear_tenant_data"
    )

    raw_result = await db.execute(
        delete(RawReading).where(
            RawReading.tenant_id == tenant_id,
            RawReading.greenhouse_id == greenhouse_id,
        )
    )
    sensor_result = await db.execute(
        delete(SensorReading).where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
        )
    )

    id_result = await db.execute(
        select(Incident.id).where(
            Incident.tenant_id == tenant_id,
            Incident.greenhouse_id == greenhouse_id,
        )
    )
    incident_ids = [row[0] for row in id_result.fetchall()]
    deleted_actions = 0
    if incident_ids:
        actions_result = await db.execute(
            delete(IncidentAction).where(IncidentAction.incident_id.in_(incident_ids))
        )
        deleted_actions = actions_result.rowcount or 0
    incidents_result = await db.execute(
        delete(Incident).where(
            Incident.tenant_id == tenant_id,
            Incident.greenhouse_id == greenhouse_id,
        )
    )

    # Feedback is keyed by customer_id (legacy name for tenant).
    feedback_result = await db.execute(
        delete(OperatorFeedback).where(OperatorFeedback.customer_id == tenant_id)
    )
    learning_result = await db.execute(
        delete(LearningLog).where(
            LearningLog.tenant_id == tenant_id,
            LearningLog.greenhouse_id == greenhouse_id,
        )
    )
    # MetricsSnapshot has no greenhouse_id column — it is a tenant-wide aggregate.
    # Intentional wider blast radius: all snapshots for the tenant are wiped so
    # stale aggregate stats cannot outlive the cleared readings they summarise.
    metrics_result = await db.execute(
        delete(MetricsSnapshot).where(MetricsSnapshot.tenant_id == tenant_id)
    )
    await db.commit()

    # Best-effort: invalidate cached readings/anomalies responses so wiped data
    # is not served from Redis for up to the 300 s TTL window.
    cache_invalidated = await invalidate_response_cache("clear_tenant_data")

    live_stream_cleared = await _clear_live_stream(
        tenant_id, greenhouse_id, "clear_tenant_data"
    )

    return {
        "cleared": True,
        "deleted_raw": raw_result.rowcount or 0,
        "deleted_sensor": sensor_result.rowcount or 0,
        "deleted_incidents": incidents_result.rowcount or 0,
        "deleted_incident_actions": deleted_actions,
        "deleted_feedback": feedback_result.rowcount or 0,
        "deleted_learning_logs": learning_result.rowcount or 0,
        "deleted_metrics_snapshots": metrics_result.rowcount or 0,
        "live_stream_cleared": live_stream_cleared,
        "live_persistence_suppressed": live_persistence_suppressed,
        "cache_invalidated": cache_invalidated,
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
    }


async def wipe_live_demo_data(
    db: AsyncSession,
    live_runtime: Any,
    tenant_id: str,
    greenhouse_id: str,
) -> dict:
    """Delete Go Live / mock SCADA persisted rows only."""
    live_persistence_suppressed = await _pause_live_scope(
        db, live_runtime, tenant_id, greenhouse_id, "remove_live_demo_data"
    )

    live_raw_filter = [
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
        or_(
            RawReading.source == LIVE_RAW_SOURCE,
            RawReading.source.like(f"{LIVE_RAW_SOURCE}:%"),
        ),
    ]

    # Only match SensorReading rows explicitly tagged source='live' by
    # PersistenceBatcher (migration 017 backfills pre-existing
    # anomaly_type='live' rows to this column). There used to be a second,
    # fallback clause here matching source IS NULL rows sharing a timestamp
    # with a live RawReading, as a proxy for "this untagged row is a
    # pre-tag live row". It is deleted, not narrowed: source IS NULL is not
    # a safe live-row proxy -- migration 017 and this wipe both landed in
    # the same commit (5922db9), so a live row written after that point was
    # already tagged, and source IS NULL on a real upload is exactly the
    # state the source-column-collision fix leaves plain CSV rows in
    # (src/config.py's CSV_UPLOAD_RAW_SOURCE docstring). Keeping the
    # fallback would delete a real CSV upload's SensorReading rows whenever
    # their timestamps happened to coincide with any live RawReading in the
    # same scope.
    sensor_result = await db.execute(
        delete(SensorReading).where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
            SensorReading.source == LIVE_RAW_SOURCE,
        )
    )

    raw_result = await db.execute(delete(RawReading).where(*live_raw_filter))
    await db.commit()

    # Best-effort: invalidate cached readings/anomalies responses so wiped data
    # is not served from Redis for up to the 300 s TTL window.
    cache_invalidated = await invalidate_response_cache("remove_live_demo_data")

    live_stream_cleared = await _clear_live_stream(
        tenant_id, greenhouse_id, "remove_live_demo_data"
    )

    return {
        "cleared_live": True,
        "deleted_raw": raw_result.rowcount or 0,
        "deleted_sensor": sensor_result.rowcount or 0,
        "live_stream_cleared": live_stream_cleared,
        "live_persistence_suppressed": live_persistence_suppressed,
        "cache_invalidated": cache_invalidated,
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
    }

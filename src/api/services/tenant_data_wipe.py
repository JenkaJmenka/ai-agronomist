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
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.cache_invalidation import invalidate_response_cache
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
    live_persistence_suppressed = _suppress_live_persistence(
        live_runtime, tenant_id, greenhouse_id, "clear_tenant_data"
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
    live_persistence_suppressed = _suppress_live_persistence(
        live_runtime, tenant_id, greenhouse_id, "remove_live_demo_data"
    )

    live_raw_filter = [
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
        or_(
            RawReading.source == LIVE_RAW_SOURCE,
            RawReading.source.like(f"{LIVE_RAW_SOURCE}:%"),
        ),
    ]
    ts_result = await db.execute(select(RawReading.timestamp).where(*live_raw_filter))
    live_timestamps = [row[0] for row in ts_result.fetchall()]

    # Primary filter: SensorReading rows tagged source='live' by PersistenceBatcher
    # (migration 017 backfills pre-existing anomaly_type='live' rows to this column).
    # Fallback: pre-tag rows (source=None, anomaly_type=None) that share a timestamp
    # with a live RawReading — never wipe inject rows (they carry a non-NULL anomaly_type).
    sensor_match = SensorReading.source == LIVE_RAW_SOURCE
    if live_timestamps:
        sensor_match = or_(
            sensor_match,
            and_(
                SensorReading.timestamp.in_(live_timestamps),
                SensorReading.source.is_(None),
                SensorReading.anomaly_type.is_(None),
            ),
        )
    sensor_result = await db.execute(
        delete(SensorReading).where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
            sensor_match,
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

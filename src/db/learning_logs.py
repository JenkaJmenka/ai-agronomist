"""
Learning history store for the online-learning subsystem (Gap 3).

Records and queries LearningLog events: observed batches, incremental
update steps, drift checks, checkpoint saves and retrain recommendations.

All methods are async (SQLAlchemy 2.x AsyncSession) and tenant-scoped.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import LearningLog

logger = logging.getLogger(__name__)

# Event type recorded by drift checks (used by get_latest_drift).
DRIFT_CHECK_EVENT: str = "drift_check"

# Default page size for history queries.
DEFAULT_HISTORY_LIMIT: int = 100

# Optional columns accepted by log_event via **fields.
_OPTIONAL_EVENT_FIELDS: frozenset = frozenset({
    "sequences_observed",
    "updates_performed",
    "loss_before",
    "loss_after",
    "drift_score",
    "drift_detected",
    "details",
    "model_checkpoint_path",
})


class LearningLogStore:
    """Tenant-scoped persistence for online-learning events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log_event(
        self,
        *,
        tenant_id: str,
        event_type: str,
        greenhouse_id: str = "default",
        **fields: Any,
    ) -> str:
        """
        Persist a learning event and return its UUID.

        Optional keyword fields: sequences_observed, updates_performed,
        loss_before, loss_after, drift_score, drift_detected, details,
        model_checkpoint_path. Unknown fields raise ValueError (fail fast).
        """
        unknown = set(fields) - _OPTIONAL_EVENT_FIELDS
        if unknown:
            raise ValueError(f"Unknown learning-log fields: {sorted(unknown)}")
        entry = LearningLog(
            tenant_id=tenant_id,
            event_type=event_type,
            greenhouse_id=greenhouse_id,
            created_at=datetime.now(timezone.utc),
            **fields,
        )
        self._session.add(entry)
        await self._session.commit()
        logger.info(
            "Logged learning event %s (%s) for tenant %s",
            entry.id, event_type, tenant_id,
        )
        return str(entry.id)

    async def get_history(
        self,
        *,
        tenant_id: str,
        greenhouse_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> List[LearningLog]:
        """Learning events for a tenant, newest first, optionally filtered."""
        query = self._base_query(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            event_type=event_type,
        ).limit(limit)
        result = await self._session.scalars(query)
        return list(result.all())

    async def get_latest_drift(
        self,
        *,
        tenant_id: str,
        greenhouse_id: Optional[str] = None,
    ) -> Optional[LearningLog]:
        """Most recent drift_check event for a tenant, or None."""
        query = self._base_query(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            event_type=DRIFT_CHECK_EVENT,
        ).limit(1)
        return await self._session.scalar(query)

    async def get_learning_summary(self, *, tenant_id: str) -> Dict[str, Any]:
        """
        Tenant-scoped learning summary.

        Returns counts per event_type, total updates performed and the
        timestamp of the most recent event (None if no events).
        """
        rows = await self._session.execute(
            select(
                LearningLog.event_type,
                func.count(LearningLog.id),
                func.sum(LearningLog.updates_performed),
                func.max(LearningLog.created_at),
            )
            .where(LearningLog.tenant_id == tenant_id)
            .group_by(LearningLog.event_type)
        )
        events_by_type: Dict[str, int] = {}
        total_updates = 0
        last_event_at: Optional[datetime] = None
        for event_type, count, updates_sum, latest in rows.all():
            events_by_type[event_type] = int(count)
            total_updates += int(updates_sum or 0)
            if latest is not None and (last_event_at is None or latest > last_event_at):
                last_event_at = latest
        return {
            "events_by_type": events_by_type,
            "total_events": sum(events_by_type.values()),
            "total_updates_performed": total_updates,
            "last_event_at": last_event_at,
        }

    @staticmethod
    def _base_query(
        *,
        tenant_id: str,
        greenhouse_id: Optional[str],
        event_type: Optional[str],
    ) -> Select:
        """Tenant-scoped query, newest first, with optional filters."""
        query = (
            select(LearningLog)
            .where(LearningLog.tenant_id == tenant_id)
            .order_by(LearningLog.created_at.desc())
        )
        if greenhouse_id is not None:
            query = query.where(LearningLog.greenhouse_id == greenhouse_id)
        if event_type is not None:
            query = query.where(LearningLog.event_type == event_type)
        return query

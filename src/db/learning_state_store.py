"""Persist and restore per-customer active learning orchestrator state."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import LearningState

logger = logging.getLogger(__name__)


class LearningStateStore:
    """DB-backed store for :class:`~src.models.retraining_orchestrator.RetrainingOrchestrator` state."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def load(self, customer_id: str) -> Optional[Dict[str, Any]]:
        """Load serialized state for a customer, or None if absent."""
        stmt = select(LearningState).where(
            LearningState.customer_id == customer_id
        )
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return dict(row.state_json)

    async def save(self, customer_id: str, state: Dict[str, Any]) -> None:
        """Upsert orchestrator state for a customer."""
        stmt = select(LearningState).where(
            LearningState.customer_id == customer_id
        ).with_for_update()
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)

        if row is None:
            self._db.add(
                LearningState(
                    customer_id=customer_id,
                    state_json=state,
                    updated_at=now,
                )
            )
        else:
            # Merge rather than replace so that other writers (e.g. ewma_pot_trackers
            # written by ComprehensiveInferencePipeline._save_trackers) are not erased.
            merged = dict(row.state_json or {})
            merged.update(state)
            row.state_json = merged
            row.updated_at = now

        await self._db.commit()
        logger.debug("Persisted learning state for customer=%s", customer_id)

    async def load_cusum_state(
        self, customer_id: str
    ) -> Optional[Dict[str, Any]]:
        """Read the ``cusum_state`` sub-key from the customer's state_json.

        Returns ``None`` when no state exists or the key is absent.  The
        returned dict can be passed directly to
        :meth:`~src.models.drift_detector.CUSUMDriftDetector.from_dict`.
        """
        stmt = select(LearningState).where(
            LearningState.customer_id == customer_id
        )
        result = await self._db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return (row.state_json or {}).get("cusum_state")

    async def save_cusum_state(
        self, customer_id: str, cusum_data: Dict[str, Any]
    ) -> None:
        """Upsert the ``cusum_state`` sub-key for a customer.

        Merges ``{"cusum_state": cusum_data}`` into the existing state_json
        without disturbing other keys (e.g. ``ewma_pot_trackers``).  The
        ``cusum_data`` dict is typically produced by
        :meth:`~src.models.drift_detector.CUSUMDriftDetector.to_dict`.
        """
        await self.save(customer_id, {"cusum_state": cusum_data})

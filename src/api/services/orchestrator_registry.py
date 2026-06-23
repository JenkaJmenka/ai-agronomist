"""
Registry for per-customer RetrainingOrchestrator instances.

In-memory cache with DB persistence via :class:`~src.db.learning_state_store.LearningStateStore`.
"""

import logging
from typing import Dict

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.learning_state_store import LearningStateStore
from src.models.retraining_orchestrator import RetrainingOrchestrator

logger = logging.getLogger(__name__)

_orchestrators: Dict[str, RetrainingOrchestrator] = {}


def get_memory_cache() -> Dict[str, RetrainingOrchestrator]:
    """Return the process-wide orchestrator cache (for pipeline wiring)."""
    return _orchestrators


def clear_orchestrator_cache() -> None:
    """Clear in-memory cache (test helper)."""
    _orchestrators.clear()


async def get_orchestrator(
    customer_id: str, db: AsyncSession
) -> RetrainingOrchestrator:
    """Load or create a per-customer orchestrator, hydrating from DB if needed."""
    if customer_id in _orchestrators:
        return _orchestrators[customer_id]

    store = LearningStateStore(db)
    state = await store.load(customer_id)
    if state is not None:
        orch = RetrainingOrchestrator(customer_id=customer_id)
        orch.load_state(state)
        logger.info("Hydrated learning state for customer=%s", customer_id)
    else:
        orch = RetrainingOrchestrator(customer_id=customer_id)

    _orchestrators[customer_id] = orch
    return orch


async def save_orchestrator(
    customer_id: str, orchestrator: RetrainingOrchestrator, db: AsyncSession
) -> None:
    """Persist orchestrator state to the database."""
    store = LearningStateStore(db)
    await store.save(customer_id, orchestrator.export_state())

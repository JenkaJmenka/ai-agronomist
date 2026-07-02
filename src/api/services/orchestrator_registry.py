"""
Registry for per-customer RetrainingOrchestrator instances.

In-memory cache with DB persistence via :class:`~src.db.learning_state_store.LearningStateStore`.
"""

import json
import logging
import os
from typing import Dict, Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.learning_state_store import LearningStateStore
from src.models.retraining_orchestrator import RetrainingOrchestrator
from src.utils.lru import LRUDict

logger = logging.getLogger(__name__)

CHECKPOINT_DIR = "data/orchestrator_checkpoints"


def _evict_orchestrator(customer_id: str, orchestrator: RetrainingOrchestrator) -> None:
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    checkpoint_path = os.path.join(CHECKPOINT_DIR, f"{customer_id}.json")
    try:
        state = orchestrator.export_state()
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        logger.info("Evicted orchestrator for customer=%s checkpointed to %s", customer_id, checkpoint_path)
    except Exception as e:
        logger.error("Failed to checkpoint evicted orchestrator for customer=%s: %s", customer_id, e)


_orchestrators = LRUDict(max_size=10, on_evict=_evict_orchestrator)


def get_memory_cache() -> LRUDict:
    """Return the process-wide orchestrator cache (for pipeline wiring)."""
    return _orchestrators


def clear_orchestrator_cache() -> None:
    """Clear in-memory cache (test helper)."""
    _orchestrators.clear()


async def get_orchestrator(
    customer_id: str, db: AsyncSession
) -> RetrainingOrchestrator:
    """Load or create a per-customer orchestrator, hydrating from disk checkpoint or DB if needed."""
    if customer_id in _orchestrators:
        return _orchestrators[customer_id]

    checkpoint_path = os.path.join(CHECKPOINT_DIR, f"{customer_id}.json")
    if os.path.exists(checkpoint_path):
        try:
            with open(checkpoint_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            orch = RetrainingOrchestrator(customer_id=customer_id)
            orch.load_state(state)
            logger.info("Restored learning state from disk checkpoint for customer=%s", customer_id)
            os.remove(checkpoint_path)
            _orchestrators[customer_id] = orch
            return orch
        except Exception as e:
            logger.warning("Failed to restore orchestrator from disk checkpoint for customer=%s: %s", customer_id, e)
            if os.path.exists(checkpoint_path):
                try:
                    os.remove(checkpoint_path)
                except Exception:
                    pass

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

"""Worker-side online-learning portion consumer.

Ownership rule (avoids split-brain): once observations flow through the
queue, the WORKER's OnlineLearningManager is the single writer of learner
state. After each portion the tenant's learner state is uploaded to the
artifact store so it survives worker restarts and is visible to the API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any, Optional

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.jobs import increment_progress, record_portion_result, set_job_result, try_transition
from src.db.learning_logs import DRIFT_CHECK_EVENT, LearningLogStore
from src.mq.schemas import OnlineTrainingPortionMessage
from src.models.artifact_store import put_checkpoint
from src.models.drift_detector import DriftDetector
from src.models.lstm_vae import LSTMVAE
from src.models.online_learner import OnlineLearningManager
from src.utils.lru import LRUDict

logger = logging.getLogger(__name__)

OBSERVE_BATCH_EVENT = "observe_batch"
ONLINE_LEARNER_KEY_PREFIX = "online_learners"

_manager: Optional[OnlineLearningManager] = None
_drift_detectors: LRUDict = LRUDict(max_size=100)


def _load_base_model() -> LSTMVAE:
    # Mirror the API's lazy base-model load (learning.py) without importing
    # the route module into the worker.
    from src.api.routes.inference import get_cached_detector

    return get_cached_detector(None).model


def _get_manager() -> OnlineLearningManager:
    global _manager
    if _manager is None:
        _manager = OnlineLearningManager(base_model=_load_base_model())
    return _manager


def _get_drift_detector(tenant_id: str) -> DriftDetector:
    if tenant_id not in _drift_detectors:
        _drift_detectors[tenant_id] = DriftDetector()
    return _drift_detectors[tenant_id]


async def _save_learner_state(db: AsyncSession, tenant_id: str) -> Optional[str]:
    manager = _get_manager()
    learner = manager.learners.get(tenant_id)
    if learner is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        learner.save_full_state(tmp_path)
        return await put_checkpoint(db, f"{ONLINE_LEARNER_KEY_PREFIX}/{tenant_id}", tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_online_portion(
    msg: OnlineTrainingPortionMessage,
    db: AsyncSession,
) -> None:
    """Observe one portion of sequences; idempotent on redelivery."""
    from sqlalchemy import func, select

    from src.db.models import JobPortion

    existing = await db.execute(
        select(func.count(JobPortion.id)).where(
            JobPortion.job_id == msg.job_id,
            JobPortion.portion_idx == msg.portion_idx,
        )
    )
    if existing.scalar():
        logger.info(
            "Online portion %s/%s already recorded — redelivery ignored",
            msg.job_id,
            msg.portion_idx,
        )
        return

    await try_transition(db, msg.job_id, "pending", "running")

    manager = _get_manager()
    detector = _get_drift_detector(msg.tenant_id)
    learner = manager.learners.get(msg.tenant_id)
    updates_before = learner.update_count if learner is not None else 0

    def _observe_all() -> tuple[list[float], Any]:
        # CPU-bound torch updates — keep off the event loop so AMQP heartbeats
        # continue flowing (a starved loop gets its broker connection reset).
        losses_acc: list[float] = []
        status = None
        for sequence in msg.sequences:
            loss = manager.observe(msg.tenant_id, np.asarray(sequence, dtype=np.float32))
            losses_acc.append(loss)
            status = detector.update(loss)
        return losses_acc, status

    losses, drift_status = await asyncio.to_thread(_observe_all)

    updates_performed = manager.learners[msg.tenant_id].update_count - updates_before

    store = LearningLogStore(db)
    await store.log_event(
        tenant_id=msg.tenant_id,
        event_type=OBSERVE_BATCH_EVENT,
        greenhouse_id=msg.greenhouse_id,
        sequences_observed=len(losses),
        updates_performed=updates_performed,
        loss_before=losses[0] if losses else None,
        loss_after=losses[-1] if losses else None,
    )
    if drift_status is not None and "warning" not in drift_status:
        await store.log_event(
            tenant_id=msg.tenant_id,
            event_type=DRIFT_CHECK_EVENT,
            greenhouse_id=msg.greenhouse_id,
            drift_score=drift_status["drift_score"],
            drift_detected=drift_status["drift_detected"],
            details=drift_status,
        )

    state_path = await _save_learner_state(db, msg.tenant_id)

    recorded = await record_portion_result(
        db,
        job_id=msg.job_id,
        portion_idx=msg.portion_idx,
        status="completed",
        result={
            "sequences_observed": len(losses),
            "updates_performed": updates_performed,
        },
    )
    if not recorded:
        return  # concurrent duplicate delivery won the insert

    completed, failed, total = await increment_progress(db, msg.job_id)
    if total is not None and completed + failed >= total:
        final_status = "partially_failed" if failed else "completed"
        await set_job_result(
            db,
            msg.job_id,
            status=final_status,
            result={
                "sequences_observed_last_portion": len(losses),
                "updates_performed_last_portion": updates_performed,
                "drift": drift_status,
                "learner_state": state_path,
                "completed_portions": completed,
                "failed_portions": failed,
            },
        )
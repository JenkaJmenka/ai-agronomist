"""Worker-side full LSTM-VAE retraining, streaming data by portions.

Data pages from the DB via keyset pagination (iter_customer_data), the
fine-tune loop consumes one portion at a time, progress lands in jobs.result,
and the improved checkpoint is uploaded to the artifact store before being
registered — the API resolves the db:// path on next inference.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import (
    ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD,
    RETRAIN_PORTION_ROWS,
    TRAINING_IF_CONTAMINATION,
)
from src.db.jobs import set_job_result, try_transition
from src.db.models import Job
from src.mq.schemas import RetrainJobMessage
from src.models.artifact_store import put_checkpoint, resolve_checkpoint
from src.models.data_utils import sanitize_training_data
from src.models.lstm_vae import LSTMVAE
from src.models.model_evaluator import ModelEvaluator
from src.models.model_registry import ModelRegistry
from src.models.training_pipeline import iter_customer_data
from src.models.transfer_learner import TransferLearner

logger = logging.getLogger(__name__)

# Rows of recent data used for the holdout comparison (bounded memory).
HOLDOUT_MAX_ROWS = 20000


async def _collect_holdout(db: AsyncSession, tenant_id: str, portion_rows: int) -> np.ndarray:
    """Bounded evaluation slice: the most recent rows, capped at HOLDOUT_MAX_ROWS."""
    collected: list[np.ndarray] = []
    total = 0
    async for portion in iter_customer_data(db, tenant_id, portion_rows):
        collected.append(portion)
        total += len(portion)
        # Keep only the tail within the cap.
        while total - len(collected[0]) >= HOLDOUT_MAX_ROWS and len(collected) > 1:
            total -= len(collected.pop(0))
    if not collected:
        return np.empty((0, 4), dtype=np.float32)
    return np.concatenate(collected)[-HOLDOUT_MAX_ROWS:]


async def handle_retrain_job(msg: RetrainJobMessage, db: AsyncSession) -> None:
    """Run one retraining job end to end."""
    claimed = await try_transition(db, msg.job_id, "pending", "running")
    if not claimed:
        # Redelivered retrain restarts are not resumable; if another run holds
        # the job, skip. (Registration below is guarded by evaluation anyway.)
        logger.info("Retrain job %s already claimed — skipping redelivery", msg.job_id)
        return

    portion_rows = msg.portion_rows or RETRAIN_PORTION_ROWS
    baseline_path = await resolve_checkpoint(db, msg.baseline_checkpoint)
    learner = TransferLearner(checkpoint_path=baseline_path) if baseline_path else TransferLearner(
        checkpoint_path=_latest_local_baseline()
    )

    def portion_factory():
        async def _gen():
            async for portion in iter_customer_data(db, msg.tenant_id, portion_rows):
                yield sanitize_training_data(
                    portion, contamination=TRAINING_IF_CONTAMINATION
                )
        return _gen()

    async def progress_cb(epoch: int, portion_idx: int, avg_loss: float) -> None:
        await set_job_result(
            db,
            msg.job_id,
            status="running",
            result={
                "progress": {
                    "epoch": epoch + 1,
                    "epochs_total": msg.epochs,
                    "portion": portion_idx,
                    "avg_loss": round(float(avg_loss), 6),
                }
            },
        )

    try:
        local_checkpoint = await learner.fine_tune_portions(
            msg.tenant_id,
            portion_factory,
            epochs=msg.epochs,
            progress_cb=progress_cb,
        )
    except ValueError as exc:
        await set_job_result(db, msg.job_id, status="failed", error=str(exc))
        return

    holdout = await _collect_holdout(db, msg.tenant_id, portion_rows)
    evaluator = ModelEvaluator()

    new_model = LSTMVAE()
    new_model.load_checkpoint(local_checkpoint)
    new_metrics = await evaluator.evaluate(new_model, holdout)

    baseline_model = LSTMVAE()
    baseline_model.load_checkpoint(learner.checkpoint_path)
    base_metrics = await evaluator.evaluate(baseline_model, holdout)

    comparison = evaluator.compare(base_metrics, new_metrics)
    logger.info(
        "Retrain %s: improvement %.2f%%", msg.tenant_id, comparison["improvement_percent"]
    )

    registry = ModelRegistry(db)
    active_model = await registry.get_active_model(msg.tenant_id)
    registered_model_id: str | None = None
    if (
        not active_model
        or comparison["improvement_percent"]
        > ACTIVE_LEARNING_IMPROVEMENT_THRESHOLD * 100
    ):
        artifact_path = await put_checkpoint(
            db, f"retrained/{msg.tenant_id}", local_checkpoint
        )
        registered_model_id = await registry.register_model(
            customer_id=msg.tenant_id,
            model_type="transfer_learned",
            checkpoint_path=artifact_path,
            baseline_id=None,
            metrics=comparison,
        )

    result: dict[str, Any] = {
        "improvement_percent": comparison["improvement_percent"],
        "registered": registered_model_id is not None,
        "model_id": registered_model_id,
        "holdout_rows": int(len(holdout)),
    }
    await set_job_result(db, msg.job_id, status="completed", result=result)


def _latest_local_baseline() -> str:
    import glob

    checkpoints = sorted(glob.glob("models/checkpoints/epoch_*.pt"))
    return checkpoints[-1] if checkpoints else "models/baseline.pt"

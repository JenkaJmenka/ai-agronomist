"""Checkpoint resolution for CSV uploads (sync and async)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.sensor_data import UploadInferenceResponse
from src.api.services.cold_start_training import (
    ColdStartTrainResult,
    should_train_cold_start,
    train_cold_start_from_csv,
)
from src.models.bootstrap import InsufficientDataError
from src.models.model_registry import ModelRegistry

logger = logging.getLogger(__name__)


class ColdStartTrainingError(RuntimeError):
    """Unexpected failure during cold-start bootstrap training."""


async def resolve_upload_checkpoint(
    db: AsyncSession,
    tenant_id: str,
    tmp_path: str,
    train_model: bool,
) -> tuple[str | None, bool, str | None, str | None]:
    """Return (checkpoint_path, model_trained, model_id, training_skipped_reason).

    Auto-training (no active model, *train_model* False) is opportunistic: on
    failure the upload proceeds against the global baseline with a skip reason.
    Forced training (*train_model* True) propagates the error to the caller.
    """
    registry = ModelRegistry(db)
    active = await registry.get_active_model(tenant_id)
    has_active = active is not None

    if not should_train_cold_start(force_train=train_model, has_active_model=has_active):
        checkpoint = active["checkpoint_path"] if active else None
        return checkpoint, False, None, "active_model_exists"

    try:
        train_result: ColdStartTrainResult = await train_cold_start_from_csv(
            db, tenant_id, tmp_path
        )
    except (InsufficientDataError, ValueError) as exc:
        if train_model:
            raise
        logger.warning(
            "Cold-start auto-training skipped for tenant %s: %s", tenant_id, exc
        )
        return None, False, None, "insufficient_data"
    except Exception as exc:
        if train_model:
            raise ColdStartTrainingError("Cold-start training failed") from exc
        logger.exception(
            "Cold-start auto-training failed for tenant %s; using baseline", tenant_id
        )
        return None, False, None, "training_error"

    return train_result.checkpoint_path, True, train_result.model_id, None


def attach_training_meta(
    response: UploadInferenceResponse,
    *,
    model_trained: bool,
    model_id: str | None,
    training_skipped_reason: str | None,
) -> UploadInferenceResponse:
    return response.model_copy(
        update={
            "model_trained": model_trained,
            "model_id": model_id,
            "training_skipped_reason": training_skipped_reason,
        }
    )

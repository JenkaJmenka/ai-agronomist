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


class NoModelAvailableError(ValueError):
    """No checkpoint could be resolved and none could be trained.

    Subclasses ``ValueError`` deliberately: both upload callers already map a
    ``ValueError`` to an actionable client-visible failure (HTTP 400 in
    ``src/api/routes/inference.py``, a failed job row in
    ``src/workers/upload_prepare_consumer.py``), so this needs no new handler
    at either call site.

    This used to be a ``return None`` whose log line said the upload would
    "proceed against the global baseline". There is no global baseline: the
    ``None`` flowed into ``get_cached_detector(None)`` and died on
    ``models/baseline.pt``, a file excluded from the image by
    ``.dockerignore``, surfacing as a misleading HTTP 503 "Failed to
    initialize model".
    """


async def resolve_upload_checkpoint(
    db: AsyncSession,
    tenant_id: str,
    tmp_path: str,
    train_model: bool,
) -> tuple[str | None, bool, str | None, str | None]:
    """Return (checkpoint_path, model_trained, model_id, training_skipped_reason).

    ``checkpoint_path`` is never ``None`` on return: a tenant with no active
    model whose cold-start training cannot produce one raises
    ``NoModelAvailableError``, because there is no baseline to fall back to.
    Forced training (*train_model* True) propagates its own error instead.

    The ``training_skipped_reason`` slot therefore only ever reports a *benign*
    skip ("active_model_exists"), not a failure.
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
            "Cold-start auto-training could not produce a model for tenant %s: %s",
            tenant_id,
            exc,
        )
        raise NoModelAvailableError(
            f"No model is deployed for tenant {tenant_id!r} and there is not "
            f"enough clean data in this upload to train one ({exc}). Upload a "
            "longer history, or retry with train_model=true once more data "
            "exists."
        ) from exc
    except Exception as exc:
        if train_model:
            raise ColdStartTrainingError("Cold-start training failed") from exc
        logger.exception(
            "Cold-start auto-training failed for tenant %s; no model available",
            tenant_id,
        )
        raise NoModelAvailableError(
            f"No model is deployed for tenant {tenant_id!r} and cold-start "
            "training failed, so this upload cannot be scored. Retry, or "
            "retry with train_model=true to surface the training error."
        ) from exc

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

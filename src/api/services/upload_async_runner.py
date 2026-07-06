"""Background pipeline for /inference/upload-async.

Cold-start training and portion publishing can exceed HTTP client timeouts on
large CSVs. The route returns 202 immediately after persisting the file and
creating a *preparing* job; this module finishes training + queueing portions.
"""

from __future__ import annotations

import logging
import os

from src.api.services.detector_cache import clear_detector_cache
from src.api.services.upload_checkpoint import (
    ColdStartTrainingError,
    resolve_upload_checkpoint,
)
from src.api.services.upload_producer import produce_upload_portions
from src.db.jobs import set_job_result
from src.db.session import AsyncSessionLocal
from src.models.bootstrap import InsufficientDataError
from src.vendors.csv_adapter import CSVAdapter

logger = logging.getLogger(__name__)


async def run_upload_async_pipeline(
    *,
    job_id: str,
    tmp_path: str,
    tenant_id: str,
    greenhouse_id: str,
    persist_data: bool,
    filename: str | None,
    train_model: bool,
) -> None:
    """Train (when needed), publish upload portions, then delete the temp file."""
    try:
        async with AsyncSessionLocal() as db:
            try:
                (
                    checkpoint_path,
                    model_trained,
                    model_id_used,
                    training_skipped_reason,
                ) = await resolve_upload_checkpoint(
                    db, tenant_id, tmp_path, train_model
                )
            except InsufficientDataError as exc:
                await set_job_result(
                    db, job_id, status="failed", error=str(exc)
                )
                return
            except ValueError as exc:
                await set_job_result(
                    db, job_id, status="failed", error=str(exc)
                )
                return
            except ColdStartTrainingError:
                logger.exception("Cold-start training failed for job %s", job_id)
                await set_job_result(
                    db,
                    job_id,
                    status="failed",
                    error="Cold-start training failed",
                )
                return

            if model_trained:
                clear_detector_cache()

            adapter = CSVAdapter({"filepath": tmp_path})
            try:
                adapter.connect()
                await produce_upload_portions(
                    tmp_path=tmp_path,
                    adapter=adapter,
                    db=db,
                    tenant_id=tenant_id,
                    greenhouse_id=greenhouse_id,
                    checkpoint_path=checkpoint_path,
                    persist_data=persist_data,
                    filename=filename,
                    model_trained=model_trained,
                    model_id=model_id_used,
                    training_skipped_reason=training_skipped_reason,
                    existing_job_id=job_id,
                )
            except Exception as exc:
                logger.exception("Upload portion publish failed for job %s", job_id)
                detail = str(exc)
                if "tokenizing" in detail.lower() or "parser" in detail.lower():
                    detail = (
                        f"Invalid CSV format: {detail}. "
                        "Re-export with comma delimiters and quote text fields "
                        "that contain commas."
                    )
                await set_job_result(db, job_id, status="failed", error=detail)
            finally:
                adapter.disconnect()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

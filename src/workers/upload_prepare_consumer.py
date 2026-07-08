"""Worker-side cold-start upload *preparation*.

Moved off the API's in-process BackgroundTasks (a Railway redeploy killed the
task and the local temp CSV mid-run, silently losing the upload) onto the
durable RabbitMQ worker. The API stores the raw CSV in the artifact store and
publishes one `UploadPrepareMessage`; this handler resolves the blob to a local
file, cold-start trains when needed, publishes scored portions, then deletes the
blob. It is idempotent: a redelivery of a crashed attempt resumes without
retraining, and a redelivery of a finished attempt is skipped.

Heartbeat safety (design O1): the only multi-minute CPU work — bootstrap
Pass1/Pass2 + LSTM-VAE training — already runs OFF the event loop via
`asyncio.to_thread(_run_pass1_pass2)` inside `train_cold_start_from_csv`, exactly
like `handle_upload_portion` wraps torch scoring. `produce_upload_portions`
yields to the loop with `await publish_json` between every ~5000-row portion, and
`adapter.connect()` only reads the file's first 4 KB. No section blocks the loop
for more than a second or two, so the 300 s AMQP heartbeat is never tripped.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.detector_cache import clear_detector_cache
from src.api.services.upload_checkpoint import (
    ColdStartTrainingError,
    resolve_upload_checkpoint,
)
from src.api.services.upload_producer import produce_upload_portions
from src.db.jobs import get_job, set_job_result, try_transition, update_job
from src.models.artifact_store import delete_upload_blob, resolve_upload_blob
from src.models.bootstrap import InsufficientDataError
from src.mq.schemas import UploadPrepareMessage
from src.utils.profiling import atimed
from src.vendors.csv_adapter import CSVAdapter

logger = logging.getLogger(__name__)

# Statuses at which preparation has already finished or is terminal, so a
# redelivered prepare message must be skipped rather than re-run.
_FINISHED_STATUSES = ("finalizing", "completed", "partially_failed", "failed")


async def _claim_or_resume(db: AsyncSession, job_id: str) -> bool:
    """Decide whether this delivery should prepare the job.

    (a) fresh job -> claim (preparing->running); (b) a prior attempt that
    crashed mid-prep (running/pending, total_portions still NULL) -> resume;
    (c) an attempt that already finished or is terminal -> skip.
    """
    if await try_transition(db, job_id, "preparing", "running"):
        return True  # fresh claim (the common path)
    job = await get_job(db, job_id)
    if job is None:
        return False
    if job.total_portions is not None or job.status in _FINISHED_STATUSES:
        return False  # prep already finished/terminal -> skip
    if job.status in ("running", "pending"):
        return True  # a prior attempt crashed mid-prep -> resume
    return False


async def _delete_blob_best_effort(db: AsyncSession, blob_path: str) -> None:
    """Delete the upload blob, logging (not raising) on failure.

    A failed delete must never fail an otherwise-successful job; the stale-prep
    sweep is the backstop that guarantees no permanent leak.
    """
    try:
        await delete_upload_blob(db, blob_path)
    except Exception:
        logger.warning("Best-effort delete of upload blob %s failed", blob_path, exc_info=True)


def _cleanup_local_file(path: str | None) -> None:
    """Remove the worker-local cache copy of the CSV (not the durable blob)."""
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            logger.warning("Could not remove worker-local CSV cache %s", path, exc_info=True)


async def handle_upload_prepare(msg: UploadPrepareMessage, db: AsyncSession) -> None:
    """Prepare one uploaded CSV: (maybe) train, publish portions, delete blob.

    Deterministic errors (bad/empty CSV, insufficient data, cold-start failure)
    fail the job, delete the blob and ACK — a retry cannot help. Any other
    (transient) error propagates to the worker's retry/DLQ machinery, which
    preserves the blob so a resumed attempt can re-read it.
    """
    if not await _claim_or_resume(db, msg.job_id):
        logger.info("Upload prepare %s already finished/claimed — skipping redelivery", msg.job_id)
        return

    try:
        local_csv = await resolve_upload_blob(db, msg.blob_path)
    except FileNotFoundError:
        # Blob gone: prep finished on a prior delivery, or was never stored.
        job = await get_job(db, msg.job_id)
        if job is not None and job.total_portions is not None:
            logger.info(
                "Upload prepare %s: blob already deleted and portions produced — skipping",
                msg.job_id,
            )
            return
        logger.error("Upload prepare %s: CSV blob %s missing — failing job", msg.job_id, msg.blob_path)
        await set_job_result(
            db,
            msg.job_id,
            status="failed",
            error="Uploaded CSV was not found in storage — please re-upload the file.",
        )
        return

    try:
        # (1) Cold-start training. Skip when a prior (crashed) attempt already
        #     trained — payload["checkpoint_path"] present => reuse it.
        job = await get_job(db, msg.job_id)
        payload = (job.payload if job else None) or {}
        if "checkpoint_path" in payload:
            checkpoint_path = payload["checkpoint_path"]
            model_trained = payload.get("model_trained", False)
            model_id = payload.get("model_id")
            skip_reason = payload.get("training_skipped_reason")
        else:
            async with atimed(
                logger, "upload.prepare.train", job=msg.job_id, train=int(msg.train_model)
            ):
                (
                    checkpoint_path,
                    model_trained,
                    model_id,
                    skip_reason,
                ) = await resolve_upload_checkpoint(
                    db, msg.tenant_id, local_csv, msg.train_model
                )
            # Persist the result BEFORE producing portions so a crash-retry does
            # NOT retrain (idempotency): resume reuses this checkpoint.
            await update_job(
                db,
                msg.job_id,
                payload={
                    **payload,
                    "checkpoint_path": checkpoint_path,
                    "model_trained": model_trained,
                    "model_id": model_id,
                    "training_skipped_reason": skip_reason,
                },
            )
            if model_trained:
                clear_detector_cache()

        # (2) Parse CSV -> publish portions -> stamp total_portions.
        adapter = CSVAdapter({"filepath": local_csv})
        adapter.connect()
        try:
            async with atimed(logger, "upload.prepare.produce_portions", job=msg.job_id):
                await produce_upload_portions(
                    tmp_path=local_csv,
                    adapter=adapter,
                    db=db,
                    tenant_id=msg.tenant_id,
                    greenhouse_id=msg.greenhouse_id,
                    checkpoint_path=checkpoint_path,
                    persist_data=msg.persist_data,
                    filename=msg.filename,
                    seq_len=msg.seq_len,
                    model_trained=model_trained,
                    model_id=model_id,
                    training_skipped_reason=skip_reason,
                    existing_job_id=msg.job_id,
                )
        finally:
            adapter.disconnect()

        # (3) Portions carry their own rows — no consumer needs the CSV again
        #     (including finalize). Delete the blob now.
        await _delete_blob_best_effort(db, msg.blob_path)

    except (InsufficientDataError, ValueError) as exc:
        detail = str(exc)
        # Preserve the CSV-parser hint from the old in-process runner.
        if "tokenizing" in detail.lower() or "parser" in detail.lower():
            detail = (
                f"Invalid CSV format: {detail}. "
                "Re-export with comma delimiters and quote text fields "
                "that contain commas."
            )
        await set_job_result(db, msg.job_id, status="failed", error=detail)
        await _delete_blob_best_effort(db, msg.blob_path)
        return  # ACK — a data/validation error will not fix on retry
    except ColdStartTrainingError:
        logger.exception("Cold-start training failed for job %s", msg.job_id)
        await set_job_result(db, msg.job_id, status="failed", error="Cold-start training failed")
        await _delete_blob_best_effort(db, msg.blob_path)
        return
    # Any *other* exception (transient DB/broker/OOM/timeout) propagates -> the
    # worker's retry/DLQ machinery handles it; the blob is preserved for resume.
    finally:
        _cleanup_local_file(local_csv)  # worker-local cache copy only


async def _terminal_upload_prepare(
    db: AsyncSession, msg: UploadPrepareMessage, error: str, attempt: int
) -> None:
    """Retries exhausted (DLQ): fail the job and clean the blob (best-effort)."""
    await set_job_result(db, msg.job_id, status="failed", error=error)
    await _delete_blob_best_effort(db, msg.blob_path)

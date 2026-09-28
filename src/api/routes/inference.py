from fastapi import APIRouter, HTTPException, Depends
from src.api.auth import enforce_tenant_access, require_api_key
from src.api.schemas.sensor_data import (
    InferenceRequest,
    InferenceResponse,
    UploadInferenceResponse,
)
from src.api.services.cold_run_upload import (
    EmptyCsvError,
    run_cold_run_upload,
)
from src.models.bootstrap import InsufficientDataError
from src.api.services.detector_cache import clear_detector_cache, get_cached_detector
from src.api.services.upload_checkpoint import (
    ColdStartTrainingError,
    attach_training_meta,
    resolve_upload_checkpoint,
)
from src.api.schemas.jobs import UploadJobAcceptedResponse
from src.db.jobs import create_job, set_job_result, update_job
from src.db.session import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from src.db.models import SensorReading
from src.models.artifact_store import put_upload_blob, resolve_checkpoint
from src.models.inference import AnomalyDetector
from src.models.model_registry import ModelRegistry
from src.mq.publisher import publish_json
from src.mq.schemas import UploadPrepareMessage
from src.mq.topology import UPLOAD_PREPARE_QUEUE
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import numpy as np
from fastapi import UploadFile, File, Form
import tempfile
import os
import logging

from src.vendors.csv_adapter import CSVAdapter
from src.api.services.score_transform import canonical as _canonical_score
from src.config import ANOMALY_THRESHOLD, ANOMALY_SCALING_FACTOR, LATENT_DIM, SCORE_SCALE_CALIBRATION, SEQ_LEN
from src.api.services.csv_persist import normalize_timestamp_key

logger = logging.getLogger(__name__)


router = APIRouter()


@router.post("/inference", response_model=InferenceResponse)
async def run_inference(
    request: InferenceRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    enforce_tenant_access(auth_tenant, request.tenant_id)
    seqs = request.sequences
    if not seqs:
        raise HTTPException(status_code=400, detail="Empty sequences")

    # Resolve active model for customer
    registry = ModelRegistry(db)
    active_model_info = await registry.get_active_model(request.tenant_id)
    
    checkpoint_path = None
    model_id_used = "baseline_v1"
    
    if active_model_info:
        checkpoint_path = active_model_info["checkpoint_path"]
        model_id_used = active_model_info["model_id"]

    try:
        # Worker-trained checkpoints live in the DB artifact store (db:// paths).
        checkpoint_path = await resolve_checkpoint(db, checkpoint_path)
        detector = get_cached_detector(checkpoint_path)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Failed to initialize model: {e}")

    try:
        # Expected shape (batch, seq_len, input_dim) -> (batch, 20, 4)
        np_seqs = [np.array(seq, dtype=np.float32) for seq in seqs]
        # Single enriched forward pass yields scores + embeddings + uncertainties
        # (fixes the previous duplicated predict_batch call)
        raw_scores, embeddings, uncertainties = detector.predict_batch_enriched(np_seqs)
        # Scale or clip raw reconstruction errors to [0, 1] range to satisfy Pydantic Schema
        batch_scores = np.clip(raw_scores / ANOMALY_SCALING_FACTOR, 0.0, 1.0)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Inference failed: {str(e)}")

    anomalies_detected = int((batch_scores > ANOMALY_THRESHOLD).sum())
    high_anomalies = [
        {"sequence_index": i, "score": float(s), "timestamp": datetime.now(timezone.utc).isoformat()}
        for i, s in enumerate(batch_scores)
        if s > ANOMALY_THRESHOLD
    ]
    
    
    # Persist scores to the matching database records for this tenant/greenhouse.
    # Persistence is opt-in and requires one timestamp per sequence — the old
    # blind positional fallback (ORDER BY timestamp DESC) mis-attributed scores
    # and was removed.
    if request.persist_scores:
        if not request.timestamps or len(request.timestamps) != len(batch_scores):
            raise HTTPException(
                status_code=400,
                detail="timestamps required (one per sequence) to persist anomaly scores",
            )

        # raw_by_key: pre-canonical scores (raw / ANOMALY_SCALING_FACTOR).
        # canonical_by_key: ADR-005 scores stored in anomaly_score column.
        raw_by_key: dict[str, float] = {}
        canonical_by_key: dict[str, float] = {}
        dt_timestamps: list[datetime] = []
        _batch_p, _batch_hi = SCORE_SCALE_CALIBRATION["batch"]
        for ts_str, score in zip(request.timestamps, batch_scores):
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unparseable timestamp {ts_str!r} for anomaly score persistence",
                ) from exc
            dt_timestamps.append(dt)
            key = normalize_timestamp_key(dt)
            if key is not None:
                raw_score = float(score)
                can = _canonical_score(raw_score, _batch_p, _batch_hi, timestamp=ts_str)
                raw_by_key[key] = raw_score
                if can is not None:
                    canonical_by_key[key] = can

        stmt = select(SensorReading).where(
            SensorReading.tenant_id == request.tenant_id,
            SensorReading.greenhouse_id == request.greenhouse_id,
            SensorReading.timestamp.in_(dt_timestamps),
        )
        result = await db.execute(stmt)
        records = result.scalars().all()

        for record in records:
            record_key = normalize_timestamp_key(record.timestamp)
            if record_key is not None and record_key in canonical_by_key:
                # ADR-005: store canonical score; preserve raw for recalibration.
                record.anomaly_score = canonical_by_key[record_key]
                record.anomaly_score_raw = raw_by_key[record_key]
                record.score_scale = "batch"

        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to persist anomaly scores: {str(e)}")



    return InferenceResponse(
        tenant_id=request.tenant_id,
        greenhouse_id=request.greenhouse_id,
        sequences_processed=len(seqs),
        anomalies_detected=anomalies_detected,
        anomaly_scores=batch_scores.tolist(),
        high_anomalies=high_anomalies,
        embeddings=np.asarray(embeddings).tolist(),
        uncertainties=np.asarray(uncertainties).tolist(),
        embedding_dim=LATENT_DIM
    )


@router.post("/inference/upload", response_model=UploadInferenceResponse)
async def run_inference_from_csv(
    tenant_id: str = Form(...),
    greenhouse_id: str = Form(default="default"),
    file: UploadFile = File(...),
    persist_data: bool = Form(default=False),
    include_embeddings: bool = Form(default=False),
    train_model: bool = Form(default=False),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    """
    Run cold-run inference on an uploaded CSV.

    When no tenant model is deployed, a cold-start model is trained from the
    upload automatically (Option A). Pass train_model=true to force retraining
    even when an active model exists.

    When persist_data is true, also loads rows into the database with
    anomaly scores attached so Monitoring and Anomaly pages work immediately.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported.")

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            while chunk := await file.read(1024 * 1024):
                tmp.write(chunk)
            tmp_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write uploaded file: {e}")

    registry = ModelRegistry(db)
    active_model_info = await registry.get_active_model(tenant_id)
    checkpoint_path = active_model_info["checkpoint_path"] if active_model_info else None
    model_trained = False
    model_id_used: str | None = None
    training_skipped_reason: str | None = None

    try:
        (
            checkpoint_path,
            model_trained,
            model_id_used,
            training_skipped_reason,
        ) = await resolve_upload_checkpoint(db, tenant_id, tmp_path, train_model)
    except InsufficientDataError as exc:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ColdStartTrainingError as exc:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if model_trained:
        clear_detector_cache()

    try:
        checkpoint_path = await resolve_checkpoint(db, checkpoint_path)
        detector = get_cached_detector(checkpoint_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=503, detail=f"Failed to initialize model: {e}")

    adapter = CSVAdapter({"filepath": tmp_path})
    try:
        adapter.connect()
        response, sequence_timestamps = await run_cold_run_upload(
            tmp_path=tmp_path,
            adapter=adapter,
            detector=detector,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            include_embeddings=include_embeddings,
            persist_db=db if persist_data else None,
        )
        return attach_training_meta(
            response,
            model_trained=model_trained,
            model_id=model_id_used,
            training_skipped_reason=training_skipped_reason,
        )
    except EmptyCsvError as e:
        raise HTTPException(status_code=400, detail=f"Uploaded CSV contains no valid data: {e}") from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Data validation failed: {e}") from e
    except RuntimeError as e:
        if "NaN in batch" in str(e):
            raise HTTPException(status_code=400, detail=f"chunk inference failed: {e}") from e
        raise HTTPException(status_code=422, detail=f"Invalid CSV format: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid CSV format: {e}") from e
    finally:
        adapter.disconnect()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.post(
    "/inference/upload-async",
    response_model=UploadJobAcceptedResponse,
    status_code=202,
)
async def run_inference_from_csv_async(
    tenant_id: str = Form(...),
    greenhouse_id: str = Form(default="default"),
    file: UploadFile = File(...),
    persist_data: bool = Form(default=False),
    train_model: bool = Form(default=False),
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
):
    """Queue a CSV for portioned processing; poll /api/jobs/{job_id} for results.

    Returns 202 immediately after the file is durably stored and a prepare
    message is published. Cold-start training (when needed) and portion
    publishing run on the RabbitMQ worker, so a redeploy can no longer lose an
    in-flight upload. The uploaded CSV is written to the artifact store (bytea)
    keyed ``uploads/<job_id>`` — the worker resolves that db:// reference since
    Railway containers share no filesystem. The local temp file is transient
    scratch on the API side only, deleted before the request returns.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported.")

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            while chunk := await file.read(1024 * 1024):
                tmp.write(chunk)
            tmp_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write uploaded file: {e}")

    payload = {
        "filename": file.filename,
        "persist_data": persist_data,
        "train_model": train_model,
    }
    try:
        job = await create_job(
            db,
            tenant_id=tenant_id,
            job_type="upload_csv",
            greenhouse_id=greenhouse_id,
            status="preparing",
            payload=payload,
        )
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=500, detail=f"Failed to create upload job: {e}") from e

    try:
        try:
            blob_path = await put_upload_blob(db, f"uploads/{job.id}", tmp_path)
        except Exception as e:
            # Job exists but has no blob; the stale-prepare sweep is sweep-safe.
            logger.exception("Failed to store upload blob for job %s", job.id)
            await set_job_result(
                db,
                job.id,
                status="failed",
                error="Failed to store the uploaded file for processing.",
            )
            raise HTTPException(status_code=500, detail=f"Failed to store uploaded file: {e}") from e

        # Record the blob ref so the sweep can clean it even if publish is lost.
        await update_job(db, job.id, payload={**payload, "blob_path": blob_path})

        try:
            await publish_json(
                UPLOAD_PREPARE_QUEUE,
                UploadPrepareMessage(
                    job_id=job.id,
                    tenant_id=tenant_id,
                    greenhouse_id=greenhouse_id,
                    blob_path=blob_path,
                    filename=file.filename,
                    persist_data=persist_data,
                    train_model=train_model,
                    seq_len=SEQ_LEN,
                ),
            )
        except Exception as e:
            # Blob is stored but the message is lost — the (time-gated) sweep
            # will fail the job and clean the blob. Surface a 500 so the client
            # knows to retry rather than silently swallowing.
            logger.exception("Failed to publish upload-prepare message for job %s", job.id)
            raise HTTPException(
                status_code=500, detail=f"Failed to queue upload for processing: {e}"
            ) from e
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return UploadJobAcceptedResponse(
        job_id=job.id,
        portion_count=0,
        poll_url=f"/api/jobs/{job.id}",
    )
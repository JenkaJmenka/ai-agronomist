from fastapi import APIRouter, HTTPException, Depends
from src.api.schemas.sensor_data import (
    InferenceRequest,
    InferenceResponse,
    UploadInferenceResponse,
)
from src.api.services.cold_run_upload import (
    EmptyCsvError,
    run_cold_run_upload,
)
from src.api.services.upload_producer import produce_upload_portions
from src.api.schemas.jobs import UploadJobAcceptedResponse
from src.db.session import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from src.db.models import SensorReading
from src.models.artifact_store import resolve_checkpoint
from src.models.inference import AnomalyDetector
from src.models.model_registry import ModelRegistry
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import numpy as np
from fastapi import UploadFile, File, Form
import tempfile
import os
import logging

from src.vendors.csv_adapter import CSVAdapter
from src.config import ANOMALY_THRESHOLD, ANOMALY_SCALING_FACTOR, LATENT_DIM, MAX_PIPELINE_CACHE_SIZE
from src.utils.lru import LRUDict
from src.api.services.csv_persist import normalize_timestamp_key

logger = logging.getLogger(__name__)


router = APIRouter()

_BASELINE_CACHE_KEY = "__baseline__"
# Single eviction policy for loaded detectors (AnomalyDetector no longer keeps
# its own class-level cache); sized by MAX_PIPELINE_CACHE_SIZE.
_detector_cache: LRUDict = LRUDict(max_size=MAX_PIPELINE_CACHE_SIZE)


def get_cached_detector(checkpoint_path: Optional[str]) -> AnomalyDetector:
    """
    LRU-cached AnomalyDetector factory to prevent unbounded model memory growth
    and reuse loaded instances across requests.
    """
    key = checkpoint_path or _BASELINE_CACHE_KEY
    try:
        return _detector_cache[key]
    except KeyError:
        detector = AnomalyDetector(checkpoint_path=checkpoint_path)
        _detector_cache[key] = detector
        return detector


def clear_detector_cache() -> None:
    """Reset the detector cache (primarily for tests)."""
    _detector_cache.data.clear()
    _detector_cache.usage.clear()

@router.post("/inference", response_model=InferenceResponse)
async def run_inference(
    request: InferenceRequest,
    db: AsyncSession = Depends(get_db)
):
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

        scores_by_key: dict[str, float] = {}
        dt_timestamps: list[datetime] = []
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
                scores_by_key[key] = float(score)

        stmt = select(SensorReading).where(
            SensorReading.tenant_id == request.tenant_id,
            SensorReading.greenhouse_id == request.greenhouse_id,
            SensorReading.timestamp.in_(dt_timestamps),
        )
        result = await db.execute(stmt)
        records = result.scalars().all()

        for record in records:
            record_key = normalize_timestamp_key(record.timestamp)
            if record_key is not None and record_key in scores_by_key:
                record.anomaly_score = scores_by_key[record_key]

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
    db: AsyncSession = Depends(get_db),
):
    """
    Run cold-run inference on an uploaded CSV.

    When persist_data is true, also loads rows into the database with
    anomaly scores attached so Monitoring and Anomaly pages work immediately.
    """
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
        return response
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
    db: AsyncSession = Depends(get_db),
):
    """Queue a CSV for portioned processing; poll /api/jobs/{job_id} for results.

    The file is parsed once and published to RabbitMQ portion-by-portion; the
    worker service scores and persists each portion and finalizes the
    file-level threshold when all portions are done.
    """
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

    adapter = CSVAdapter({"filepath": tmp_path})
    try:
        adapter.connect()
        job_id, portion_count = await produce_upload_portions(
            tmp_path=tmp_path,
            adapter=adapter,
            db=db,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            checkpoint_path=checkpoint_path,
            persist_data=persist_data,
            filename=file.filename,
        )
        return UploadJobAcceptedResponse(
            job_id=job_id,
            portion_count=portion_count,
            poll_url=f"/api/jobs/{job_id}",
        )
    except EmptyCsvError as e:
        raise HTTPException(status_code=400, detail=f"Uploaded CSV contains no valid data: {e}") from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Data validation failed: {e}") from e
    finally:
        adapter.disconnect()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
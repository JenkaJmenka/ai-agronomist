from fastapi import APIRouter, HTTPException, Depends
from src.api.schemas.sensor_data import (
    InferenceRequest,
    InferenceResponse,
    UploadInferenceResponse,
)
from src.api.services.cold_run_upload import (
    build_anomaly_metadata_by_timestamp,
    build_anomaly_scores_by_timestamp,
    run_cold_run_upload,
)
from src.api.services.csv_persist import persist_csv_file
from src.db.session import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from src.db.models import SensorReading
from src.models.inference import AnomalyDetector
from src.models.model_registry import ModelRegistry
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import numpy as np
from fastapi import UploadFile, File, Form
import tempfile
import os
import logging
from functools import lru_cache

from src.vendors.csv_adapter import CSVAdapter
from src.config import ANOMALY_THRESHOLD, ANOMALY_SCALING_FACTOR, LATENT_DIM, MAX_PIPELINE_CACHE_SIZE

logger = logging.getLogger(__name__)


router = APIRouter()

@lru_cache(maxsize=MAX_PIPELINE_CACHE_SIZE)
def get_cached_detector(checkpoint_path: Optional[str]) -> AnomalyDetector:
    """
    LRU cached AnomalyDetector factory to prevent unbounded model memory leaks and reuse instances.
    """
    return AnomalyDetector(checkpoint_path=checkpoint_path)

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
    
    
    # Persist scores to the matching database records for this tenant/greenhouse
    if getattr(request, "timestamps", None) is not None and len(request.timestamps) == len(batch_scores):
        dt_timestamps = []
        for ts_str in request.timestamps:
            try:
                dt_timestamps.append(datetime.fromisoformat(ts_str.replace('Z', '+00:00')))
            except Exception as exc:
                logger.warning(
                    "Could not parse timestamp %r for anomaly score persistence: %s",
                    ts_str, exc,
                )
                
        if dt_timestamps:
            stmt = select(SensorReading).where(
                SensorReading.tenant_id == request.tenant_id,
                SensorReading.greenhouse_id == request.greenhouse_id,
                SensorReading.timestamp.in_(dt_timestamps)
            )
            result = await db.execute(stmt)
            records = result.scalars().all()
            
            for record in records:
                for idx, dt in enumerate(dt_timestamps):
                    if record.timestamp == dt or record.timestamp.replace(tzinfo=timezone.utc) == dt.replace(tzinfo=timezone.utc):
                        record.anomaly_score = float(batch_scores[idx])
                        break
    else:
        # Fallback to the old blind-matching logic if no timestamps provided
        stmt = select(SensorReading).where(
            SensorReading.tenant_id == request.tenant_id,
            SensorReading.greenhouse_id == request.greenhouse_id,
        ).order_by(SensorReading.timestamp.desc()).limit(len(batch_scores))
        
        result = await db.execute(stmt)
        records = result.scalars().all()
        
        # We update records with anomaly scores starting from the matching indices
        for idx, record in enumerate(reversed(records)):
            if idx < len(batch_scores):
                record.anomaly_score = float(batch_scores[idx])

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
        detector = get_cached_detector(checkpoint_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=503, detail=f"Failed to initialize model: {e}")

    adapter = CSVAdapter({"filepath": tmp_path})
    try:
        adapter.connect()
        response, sequence_timestamps = run_cold_run_upload(
            tmp_path=tmp_path,
            adapter=adapter,
            detector=detector,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
        )
        if persist_data:
            score_map = build_anomaly_scores_by_timestamp(response.high_anomalies)
            metadata_map = build_anomaly_metadata_by_timestamp(response.high_anomalies)
            persist_result = await persist_csv_file(
                tmp_path=tmp_path,
                tenant_id=tenant_id,
                greenhouse_id=greenhouse_id,
                db=db,
                adapter=adapter,
                anomaly_scores_by_timestamp=score_map,
                anomaly_metadata_by_timestamp=metadata_map,
            )
            response = response.model_copy(
                update={
                    "data_persisted": True,
                    "rows_uploaded": persist_result.sensor_rows or persist_result.raw_rows,
                    "sensor_rows_uploaded": persist_result.sensor_rows,
                    "raw_rows_uploaded": persist_result.raw_rows,
                }
            )
        return response
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Data validation failed: {e}") from e
    finally:
        adapter.disconnect()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
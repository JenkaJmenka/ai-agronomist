from fastapi import APIRouter, HTTPException, Depends
from src.api.schemas.sensor_data import InferenceRequest, InferenceResponse
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
import pandas as pd
from functools import lru_cache

from src.vendors.csv_adapter import CSVAdapter
from src.models.data_utils import preprocess_sequences, normalize_data, SENSOR_RANGES
from src.models.postprocessing import (
    map_timestamp_to_anomaly,
    clip_and_scale_anomalies,
    get_high_anomaly_timestamps,
)
from src.config import ANOMALY_THRESHOLD, ANOMALY_SCALING_FACTOR, LATENT_DIM


router = APIRouter()

@lru_cache(maxsize=16)
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
            except Exception:
                pass
                
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


@router.post("/inference/upload")
async def run_inference_from_csv(
    tenant_id: str = Form(...),
    greenhouse_id: str = Form(default="default"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """
    Directly runs inference on an uploaded CSV file without persisting the raw data,
    processing via chunked DataFrames to prevent memory exhaustion, and utilizing
    robust timestamp mapping.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported.")

    # Write file asynchronously using chunk reads to prevent memory exhaustion
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            while chunk := await file.read(1024 * 1024):  # 1MB buffer chunk
                tmp.write(chunk)
            tmp_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write uploaded file: {e}")

    # Retrieve customer's active model
    registry = ModelRegistry(db)
    active_model_info = await registry.get_active_model(tenant_id)
    
    checkpoint_path = None
    if active_model_info:
        checkpoint_path = active_model_info["checkpoint_path"]
        
    try:
        detector = get_cached_detector(checkpoint_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=503, detail=f"Failed to initialize model: {e}")

    adapter = CSVAdapter({"filepath": tmp_path})
    try:
        adapter.connect()
    except Exception as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise HTTPException(status_code=422, detail=f"Invalid CSV format: {e}")

    seq_len = 20
    chunk_size = 5000

    anomaly_scores: List[float] = []
    embeddings: List[List[float]] = []
    uncertainties: List[float] = []
    high_anomalies: List[Dict[str, Any]] = []
    sequences_processed = 0
    anomalies_detected = 0

    prev_overlap_df = pd.DataFrame()
    global_seq_index = 0
    has_valid_data = False

    try:
        for raw_chunk in pd.read_csv(tmp_path, chunksize=chunk_size):
            if raw_chunk.empty:
                continue

            try:
                normalized_chunk = adapter.normalize(raw_chunk)
                adapter.validate(normalized_chunk)
            except Exception as e:
                # Distinguish from other validation failures
                raise HTTPException(status_code=422, detail=f"Data validation failed: {str(e)}")

            has_valid_data = True

            if not prev_overlap_df.empty:
                combined_df = pd.concat([prev_overlap_df, normalized_chunk], ignore_index=True)
            else:
                combined_df = normalized_chunk

            # Update the overlap (last seq_len - 1 elements) for the next iteration
            if len(normalized_chunk) >= seq_len - 1:
                prev_overlap_df = normalized_chunk.iloc[-(seq_len - 1):].copy()
            else:
                if len(combined_df) >= seq_len - 1:
                    prev_overlap_df = combined_df.iloc[-(seq_len - 1):].copy()
                else:
                    prev_overlap_df = combined_df.copy()

            if len(combined_df) < seq_len:
                continue

            try:
                raw_data = combined_df[["temperature", "humidity", "co2", "light"]].to_numpy()
            except KeyError as e:
                raise HTTPException(status_code=422, detail=f"Missing required columns in chunk: {e}")

            norm_data = normalize_data(raw_data, SENSOR_RANGES)

            try:
                sequences = preprocess_sequences(norm_data, seq_len=seq_len)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))

            try:
                # Single enriched forward pass per chunk: scores + embeddings + uncertainties
                raw_scores, chunk_embeddings, chunk_uncertainties = detector.predict_batch_enriched(sequences)
                batch_scores = np.clip(raw_scores / ANOMALY_SCALING_FACTOR, 0.0, 1.0)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Inference failed during chunk processing: {str(e)}")

            anomalies_detected += int((batch_scores > ANOMALY_THRESHOLD).sum())
            sequences_processed += len(sequences)
            anomaly_scores.extend(batch_scores.tolist())
            embeddings.extend(np.asarray(chunk_embeddings).tolist())
            uncertainties.extend(np.asarray(chunk_uncertainties).tolist())

            # Perform robust timestamp mapping using sequence indices
            timestamps = combined_df["timestamp"].tolist()
            for idx, score in enumerate(batch_scores):
                row_idx = idx + seq_len - 1
                if row_idx < len(timestamps):
                    ts_val = timestamps[row_idx]
                    if pd.isnull(ts_val):
                        ts = "unknown"
                    else:
                        ts = ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val)
                else:
                    ts = "unknown"

                if score > ANOMALY_THRESHOLD:
                    high_anomalies.append({
                        "sequence_index": global_seq_index + idx,
                        "score": float(score),
                        "timestamp": ts
                    })

            global_seq_index += len(sequences)

    finally:
        adapter.disconnect()
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if not has_valid_data:
        raise HTTPException(status_code=400, detail="Uploaded CSV contains no valid data.")

    return {
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
        "sequences_processed": sequences_processed,
        "anomalies_detected": anomalies_detected,
        "anomaly_scores": anomaly_scores,
        "high_anomalies": high_anomalies,
        "embeddings": embeddings,
        "uncertainties": uncertainties,
        "embedding_dim": LATENT_DIM
    }
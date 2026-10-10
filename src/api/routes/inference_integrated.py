"""
Integrated comprehensive inference API routes.

POST /api/inference/comprehensive -- full anomaly pipeline
POST /api/inference/batch       -- batch comprehensive inference
"""

import logging
from collections import OrderedDict

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.services.detector_cache import (
    NoActiveModelError,
    ResolvedDetector,
    resolve_tenant_detector,
)
from src.config import MAX_PIPELINE_CACHE_SIZE
from src.api.services.orchestrator_registry import get_memory_cache
from src.api.schemas.inference_integrated import (
    BatchInferenceRequest,
    BatchInferenceResponse,
    ComprehensiveInferenceRequest,
    ComprehensiveInferenceResponse,
    LearningProgressSummary,
)
from src.api.services.comprehensive_pipeline import ComprehensiveInferencePipeline
from src.db.session import get_db
from src.monitoring.anomaly_metrics import get_anomaly_metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/inference", tags=["inference-integrated"])

_pipeline_cache: "OrderedDict[str, ComprehensiveInferencePipeline]" = OrderedDict()


def _get_pipeline(resolved: ResolvedDetector) -> ComprehensiveInferencePipeline:
    """Cache the pipeline by the *resolved* (post artifact-store) checkpoint
    path. Two different db:// artifacts always resolve to two different
    paths, so they can't collide here; the same artifact always resolves to
    the same path, so cache hits still work exactly as before.
    """
    cache_key = resolved.checkpoint_path or "__baseline__"
    if cache_key in _pipeline_cache:
        _pipeline_cache.move_to_end(cache_key)
        return _pipeline_cache[cache_key]
    pipeline = ComprehensiveInferencePipeline(
        detector=resolved.detector,
        metrics=get_anomaly_metrics(),
        orchestrators=get_memory_cache(),
    )
    _pipeline_cache[cache_key] = pipeline
    if len(_pipeline_cache) > MAX_PIPELINE_CACHE_SIZE:
        _pipeline_cache.popitem(last=False)
    return pipeline


async def _resolve_pipeline(
    customer_id: str, db: AsyncSession
) -> ComprehensiveInferencePipeline:
    """Resolve the tenant's active model and return its cached pipeline.

    Delegates to detector_cache.resolve_tenant_detector (also used by
    /api/backfill/scores) instead of duplicating the ModelRegistry +
    resolve_checkpoint glue a further time — that duplication is exactly how
    this router shipped with db:// checkpoints never resolved (see
    NoActiveModelError below for why "no active model" is now a hard
    failure rather than a silent baseline fallback).
    """
    resolved = await resolve_tenant_detector(db, customer_id)
    return _get_pipeline(resolved)


def _to_response(result: dict) -> ComprehensiveInferenceResponse:
    progress = result.get("learning_progress")
    learning = (
        LearningProgressSummary(**progress) if progress is not None else None
    )
    return ComprehensiveInferenceResponse(
        is_anomaly=result["is_anomaly"],
        anomaly_score=result["anomaly_score"],
        confidence=result["confidence"],
        confidence_level=result["confidence_level"],
        pattern_type=result["pattern_type"],
        attribution=result["attribution"],
        affected_sensors=result["affected_sensors"],
        pattern_scores=result["pattern_scores"],
        root_cause=result["root_cause"],
        root_cause_confidence=result["root_cause_confidence"],
        reasoning=result["reasoning"],
        alternative_causes=result["alternative_causes"],
        severity=result["severity"],
        urgency=result["urgency"],
        immediate_actions=result["immediate_actions"],
        follow_up_actions=result["follow_up_actions"],
        all_recommendations=result["all_recommendations"],
        requires_technician=result["requires_technician"],
        estimated_resolution_minutes=result["estimated_resolution_minutes"],
        incident_id=result["incident_id"],
        detector_scores=result.get("detector_scores"),
        ensemble_score=result.get("ensemble_score"),
        ensemble_agreement=result.get("ensemble_agreement"),
        learning_progress=learning,
        timestamp=result["timestamp"],
        pipeline_version=result["pipeline_version"],
        scada_anomalies=result.get("scada_anomalies") or [],
    )


@router.post(
    "/comprehensive",
    response_model=ComprehensiveInferenceResponse,
    status_code=status.HTTP_200_OK,
)
async def comprehensive_inference(
    request: ComprehensiveInferenceRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> ComprehensiveInferenceResponse:
    """Run the full anomaly detection pipeline for one sequence."""
    enforce_tenant_access(auth_tenant, request.customer_id)
    try:
        pipeline = await _resolve_pipeline(request.customer_id, db)
        sensor_pairs_raw = None
        if request.sensor_pairs:
            sensor_pairs_raw = [
                {"variable": sp.variable, "sensor_a": sp.sensor_a, "sensor_b": sp.sensor_b}
                for sp in request.sensor_pairs
            ]
        result = await pipeline.run(
            customer_id=request.customer_id,
            sequence=request.sequence,
            db=db,
            greenhouse_id=request.greenhouse_id,
            baseline_sequences=request.baseline_sequences,
            sensor_pairs=sensor_pairs_raw,
            sequence_timestamp=request.sequence_timestamp,
            climate_context=request.climate_context,
        )
        return _to_response(result)
    except NoActiveModelError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Comprehensive inference failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inference failed: {exc}",
        ) from exc


@router.post(
    "/batch",
    response_model=BatchInferenceResponse,
    status_code=status.HTTP_200_OK,
)
async def batch_inference(
    request: BatchInferenceRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> BatchInferenceResponse:
    """Run comprehensive inference for multiple sequences."""
    enforce_tenant_access(auth_tenant, request.customer_id)
    try:
        pipeline = await _resolve_pipeline(request.customer_id, db)

        raw_results = await pipeline.run_batch(
            customer_id=request.customer_id,
            sequences=[
                np.array(seq, dtype=np.float32) for seq in request.sequences
            ],
            db=db,
            greenhouse_id=request.greenhouse_id,
        )
        results = [_to_response(r) for r in raw_results]

        anomaly_count = sum(1 for r in results if r.is_anomaly)
        avg_confidence = float(
            np.mean([r.confidence for r in results]) if results else 0.0
        )

        return BatchInferenceResponse(
            batch_size=len(results),
            anomalies_detected=anomaly_count,
            avg_confidence=avg_confidence,
            results=results,
        )
    except NoActiveModelError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Batch inference failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Batch inference failed: {exc}",
        ) from exc

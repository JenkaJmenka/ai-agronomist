"""
API endpoints for the online-learning pipeline (Gap 3).

Streams tenant sensor sequences into per-tenant OnlineLearners (continuous
fine-tuning of the pretrained LSTM-VAE), tracks data drift per tenant, and
persists an audit trail of learning events via the LearningLogStore.
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import MAX_HISTORY_LIMIT
from src.api.routes.inference import get_cached_detector
from src.api.schemas.learning import (
    ActiveModelVersionResponse,
    DriftStateResponse,
    DriftStatus,
    LearnerStatisticsResponse,
    LearningHistoryEntry,
    LearningSummaryResponse,
    ObserveRequest,
    ObserveResponse,
    PredictRequest,
    PredictResponse,
)
from src.models.model_registry import ModelRegistry
from src.db.learning_logs import (
    DEFAULT_HISTORY_LIMIT,
    DRIFT_CHECK_EVENT,
    LearningLogStore,
)
from src.db.session import get_db
from src.models.drift_detector import DriftDetector
from src.models.lstm_vae import LSTMVAE
from src.models.online_learner import OnlineLearningManager
from src.utils.lru import LRUDict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/learning", tags=["learning"])

# Event type recorded for each observed batch.
OBSERVE_BATCH_EVENT: str = "observe_batch"

# Lazily-initialized singleton manager (one OnlineLearner per tenant) and the
# per-tenant drift detectors that track each tenant's loss stream.
_manager: Optional[OnlineLearningManager] = None
_drift_detectors: LRUDict = LRUDict(max_size=100)


def _load_base_model() -> LSTMVAE:
    """
    Load the pretrained base LSTM-VAE through the same cached checkpoint
    mechanism the inference routes use (latest baseline checkpoint).
    """
    try:
        return get_cached_detector(None).model
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to initialize base model: {exc}",
        )


def _get_manager() -> OnlineLearningManager:
    """Return the singleton OnlineLearningManager, creating it on first use."""
    global _manager
    if _manager is None:
        _manager = OnlineLearningManager(base_model=_load_base_model())
    return _manager


def _get_drift_detector(tenant_id: str) -> DriftDetector:
    """Return the tenant's drift detector, creating it on first use."""
    if tenant_id not in _drift_detectors:
        _drift_detectors[tenant_id] = DriftDetector()
    return _drift_detectors[tenant_id]


@router.post("/observe", response_model=ObserveResponse)
async def observe_sequences(
    request: ObserveRequest,
    db: AsyncSession = Depends(get_db),
) -> ObserveResponse:
    """Feed a batch of sequences to the tenant's online learner."""
    manager = _get_manager()
    detector = _get_drift_detector(request.tenant_id)

    learner = manager.learners.get(request.tenant_id)
    updates_before = learner.update_count if learner is not None else 0

    losses: List[float] = []
    drift_status: Optional[Dict[str, Any]] = None
    for sequence in request.sequences:
        loss = manager.observe(
            request.tenant_id, np.asarray(sequence, dtype=np.float32)
        )
        losses.append(loss)
        drift_status = detector.update(loss)

    updates_performed = (
        manager.learners[request.tenant_id].update_count - updates_before
    )

    store = LearningLogStore(db)
    await store.log_event(
        tenant_id=request.tenant_id,
        event_type=OBSERVE_BATCH_EVENT,
        greenhouse_id=request.greenhouse_id,
        sequences_observed=len(losses),
        updates_performed=updates_performed,
        loss_before=losses[0],
        loss_after=losses[-1],
    )

    # The detector only evaluates drift once it has historical data; until
    # then it returns a warning and there is nothing meaningful to persist.
    if drift_status is not None and "warning" not in drift_status:
        await store.log_event(
            tenant_id=request.tenant_id,
            event_type=DRIFT_CHECK_EVENT,
            greenhouse_id=request.greenhouse_id,
            drift_score=drift_status["drift_score"],
            drift_detected=drift_status["drift_detected"],
            details=drift_status,
        )

    return ObserveResponse(
        losses=losses,
        sequences_observed=len(losses),
        updates_performed=updates_performed,
        drift=DriftStatus(**drift_status) if drift_status is not None else None,
    )


@router.post("/jobs/observe", status_code=202)
async def observe_sequences_async(
    request: ObserveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Queue observations for portion-wise online learning on the worker.

    Once observations flow through the queue, the worker owns learner state
    (single writer); the sync /observe endpoint remains for small interactive
    batches and tests. Poll /api/jobs/{job_id} for progress.
    """
    from src.api.schemas.jobs import JobAcceptedResponse
    from src.config import ONLINE_PORTION_SEQUENCES
    from src.db.jobs import create_job
    from src.mq.publisher import publish_json
    from src.mq.schemas import OnlineTrainingPortionMessage
    from src.mq.topology import TRAINING_ONLINE_QUEUE

    sequences = request.sequences
    portion_count = max(1, -(-len(sequences) // ONLINE_PORTION_SEQUENCES))

    job = await create_job(
        db,
        tenant_id=request.tenant_id,
        job_type="training_online",
        greenhouse_id=request.greenhouse_id,
        total_portions=portion_count,
        payload={"sequences_total": len(sequences)},
    )
    for idx in range(portion_count):
        start = idx * ONLINE_PORTION_SEQUENCES
        await publish_json(
            TRAINING_ONLINE_QUEUE,
            OnlineTrainingPortionMessage(
                job_id=job.id,
                tenant_id=request.tenant_id,
                greenhouse_id=request.greenhouse_id,
                portion_idx=idx,
                portion_count=portion_count,
                sequences=sequences[start : start + ONLINE_PORTION_SEQUENCES],
            ),
        )
    return JobAcceptedResponse(
        job_id=job.id, portion_count=portion_count, poll_url=f"/api/jobs/{job.id}"
    )


@router.post("/predict", response_model=PredictResponse)
async def predict_sequence(request: PredictRequest) -> PredictResponse:
    """Score one sequence with the tenant's adapted model (or the base model)."""
    manager = _get_manager()
    result = manager.predict(
        request.tenant_id, np.asarray(request.sequence, dtype=np.float32)
    )
    return PredictResponse(
        anomaly_score=result["anomaly_score"],
        embedding=np.asarray(result["embedding"], dtype=float).tolist(),
        uncertainty=result["uncertainty"],
        trend=result.get("trend"),
        updates_performed=result.get("updates_performed", 0),
        learner_active=result["learner_active"],
    )


@router.get("/statistics", response_model=LearnerStatisticsResponse)
async def get_statistics(
    tenant_id: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
) -> LearnerStatisticsResponse:
    """Live learner statistics merged with the persisted learning summary."""
    learner_stats = _get_manager().get_tenant_statistics(tenant_id)
    summary = await LearningLogStore(db).get_learning_summary(tenant_id=tenant_id)
    return LearnerStatisticsResponse(
        tenant_id=tenant_id,
        learner=learner_stats,
        summary=LearningSummaryResponse(**summary),
    )


@router.get("/history", response_model=List[LearningHistoryEntry])
async def get_history(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
    db: AsyncSession = Depends(get_db),
) -> List[LearningHistoryEntry]:
    """Learning events for a tenant, newest first, optionally filtered."""
    entries = await LearningLogStore(db).get_history(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        event_type=event_type,
        limit=limit,
    )
    return [LearningHistoryEntry.model_validate(entry) for entry in entries]


@router.get("/drift", response_model=DriftStateResponse)
async def get_drift(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> DriftStateResponse:
    """Latest persisted drift check plus live detector state for a tenant."""
    latest = await LearningLogStore(db).get_latest_drift(
        tenant_id=tenant_id, greenhouse_id=greenhouse_id
    )
    detector = _drift_detectors.get(tenant_id)
    if latest is None and detector is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No drift data for tenant '{tenant_id}'",
        )

    detector_state: Optional[Dict[str, Any]] = None
    if detector is not None:
        detector_state = {
            "window_size": detector.window_size,
            "threshold": detector.threshold,
            "losses_tracked": len(detector.losses),
        }

    return DriftStateResponse(
        tenant_id=tenant_id,
        latest_check=(
            LearningHistoryEntry.model_validate(latest) if latest is not None else None
        ),
        detector=detector_state,
    )

@router.get("/models/{tenant_id}/active-version", response_model=ActiveModelVersionResponse)
async def get_active_model_version(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
) -> ActiveModelVersionResponse:
    """Return the currently deployed model version for a tenant.

    Returns 404 when no ModelDeployment with is_current=True exists for the tenant.
    """
    registry = ModelRegistry(db)
    active = await registry.get_active_model(tenant_id)
    if active is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active model deployment for tenant '{tenant_id}'",
        )
    return ActiveModelVersionResponse(
        model_id=active["model_id"],
        checkpoint_path=active["checkpoint_path"],
    )


@router.delete("/models")
async def clear_models(
    tenant_id: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
):
    """Clears all trained models for a tenant, resetting them to the base model."""
    import os
    from src.models.model_registry import ModelRegistry
    registry = ModelRegistry(db)
    count = await registry.clear_models(tenant_id)
    
    # Also clear the online learner from memory so it gets recreated from baseline
    global _manager
    if _manager is not None and tenant_id in _manager.learners:
        del _manager.learners[tenant_id]

    # Delete online learner checkpoint from disk if exists
    checkpoint_path = f"models/checkpoints/online_learners/{tenant_id}.pt"
    if os.path.exists(checkpoint_path):
        try:
            os.remove(checkpoint_path)
        except OSError as e:
            logger.warning("Failed to delete checkpoint file %s: %s", checkpoint_path, e)

    # Clear drift detector from memory cache
    if tenant_id in _drift_detectors:
        del _drift_detectors[tenant_id]

    from src.api.routes.inference import clear_detector_cache
    clear_detector_cache()
        
    return {"status": "success", "deleted_versions": count, "message": f"Successfully cleared {count} model(s) for tenant {tenant_id}"}

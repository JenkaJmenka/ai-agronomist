"""
API endpoints for the active learning feedback loop (Phase 3.1).

Operators mark incidents as true/false anomalies, record whether past
feedback was accurate (updating EMA reliability), and query learning
progress. When retraining criteria are met, a Phase 1 retrain cycle
is triggered automatically.

Follows the same async pattern as ``src/api/routes/incidents.py``:
``async def``, Pydantic v2.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.schemas.feedback import (
    FeedbackMarkRequest,
    FeedbackMarkResponse,
    LearningProgressResponse,
    RecordOutcomeRequest,
    RecordOutcomeResponse,
    StatisticsResponse,
)
from src.api.services.orchestrator_registry import (
    get_orchestrator,
    save_orchestrator,
)
from src.config import ACTIVE_LEARNING_MIN_QUALITY_LABELS
from src.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


@router.post("/mark", response_model=FeedbackMarkResponse)
async def mark_feedback(
    request: FeedbackMarkRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> FeedbackMarkResponse:
    """Operator marks an incident as true anomaly or false positive.

    Automatically triggers learning if retraining criteria are met.
    """
    enforce_tenant_access(auth_tenant, request.customer_id)
    orch = await get_orchestrator(request.customer_id, db)

    result = orch.observe(
        incident_id=request.incident_id,
        is_true_anomaly=request.is_true_anomaly,
        operator_id=request.operator_id,
        pattern_type_observed=request.pattern_type_observed,
        actual_root_cause=request.actual_root_cause,
        operator_confidence=request.operator_confidence,
        severity=request.severity,
        notes=request.notes,
    )
    await save_orchestrator(request.customer_id, orch, db)

    return FeedbackMarkResponse(
        status=result["status"],
        incident_id=request.incident_id,
        learning_triggered=result["learning_triggered"],
        feedback_count=result["feedback_count"],
        high_quality_count=result["high_quality_count"],
        retraining=result.get("retraining"),
    )


@router.get(
    "/statistics/{tenant_id}", response_model=StatisticsResponse
)
async def feedback_statistics(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> StatisticsResponse:
    """Return feedback statistics and learning progress for a tenant."""
    enforce_tenant_access(auth_tenant, tenant_id)
    orch = await get_orchestrator(tenant_id, db)
    stats = orch.learner.get_statistics()
    if stats["total_feedback"] == 0:
        return StatisticsResponse(
            customer_id=tenant_id,
            total_feedback=0,
            high_quality_feedback=0,
            operator_count=0,
            retrainings_completed=0,
            last_retrain=None,
            operator_reliability={},
        )

    return StatisticsResponse(**stats)


@router.get(
    "/learning-progress/{tenant_id}",
    response_model=LearningProgressResponse,
)
async def learning_progress(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> LearningProgressResponse:
    """Return detailed progress toward the next retrain trigger."""
    enforce_tenant_access(auth_tenant, tenant_id)
    orch = await get_orchestrator(tenant_id, db)
    stats = orch.learner.get_statistics()
    if stats["total_feedback"] == 0:
        return LearningProgressResponse(
            customer_id=tenant_id,
            progress_percent=0.0,
            samples_needed=ACTIVE_LEARNING_MIN_QUALITY_LABELS,
            high_quality_count=0,
            total_feedback=0,
            last_retrain=None,
        )

    progress = orch.get_progress()
    return LearningProgressResponse(**progress)


@router.post("/record-outcome", response_model=RecordOutcomeResponse)
async def record_outcome(
    request: RecordOutcomeRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> RecordOutcomeResponse:
    """Record whether an operator's past feedback was accurate.

    Updates the operator's EMA reliability score in the per-customer
    orchestrator.
    """
    enforce_tenant_access(auth_tenant, request.customer_id)
    orch = await get_orchestrator(request.customer_id, db)
    orch.learner.validator.record_feedback_outcome(
        operator_id=request.operator_id,
        feedback_was_correct=request.feedback_was_correct,
    )
    await save_orchestrator(request.customer_id, orch, db)
    new_reliability = orch.learner.validator.get_operator_reliability(
        request.operator_id
    )

    return RecordOutcomeResponse(
        status="outcome_recorded",
        operator_id=request.operator_id,
        new_reliability=new_reliability,
    )

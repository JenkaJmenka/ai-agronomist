"""
Integrated feedback API: wires operator feedback to DB incidents and learning.

POST /api/feedback/mark-anomaly — record feedback, update incident, auto-retrain
"""

import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.schemas.feedback_integrated import (
    MarkAnomalyIntegratedRequest,
    MarkAnomalyIntegratedResponse,
)
from src.api.services.orchestrator_registry import (
    get_orchestrator,
    save_orchestrator,
)
from src.config import ACTIVE_LEARNING_MIN_QUALITY_LABELS
from src.db.models import Incident
from src.db.session import get_db
from src.monitoring.anomaly_metrics import get_anomaly_metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feedback", tags=["feedback-integrated"])

CONFIRMED_TAG = "confirmed_anomaly"
FALSE_POSITIVE_TAG = "false_positive"


def _observe_online_learner_from_feedback(
    customer_id: str,
    incident: Any,
    is_anomaly: bool,
) -> None:
    """Feed a human-confirmed label to the per-tenant online learner.

    Normal sequences (is_anomaly=False) are fed so the learner sees the full
    distribution, not just anomalies.  When the incident has a stored raw
    sequence the learner receives the full (seq_len, input_dim) array; when
    only a latent embedding is available the embedding is wrapped as a proxy
    sequence so the learner still gets a gradient signal.
    """
    # Import here to avoid a circular import at module load time.
    from src.api.routes.learning import _get_manager  # noqa: PLC0415

    manager = _get_manager()

    raw_sequence = getattr(incident, "sequence", None)
    if raw_sequence is not None:
        # Incident.sequence is stored as a list-of-lists (JSON).
        sequence = np.asarray(raw_sequence, dtype=np.float32)
        manager.observe(customer_id, sequence)
        logger.debug(
            "Online learner received feedback sequence for tenant %s "
            "(incident=%s, is_anomaly=%s)",
            customer_id,
            getattr(incident, "id", "unknown"),
            is_anomaly,
        )
        return

    embedding = getattr(incident, "embedding", None)
    if embedding is not None:
        # Use the latent embedding as a single-step proxy sequence so the
        # learner still observes the incident even without the raw data.
        proxy_sequence = np.array(embedding, dtype=np.float32).reshape(1, -1)
        manager.observe(customer_id, proxy_sequence)
        logger.debug(
            "Online learner received proxy embedding for tenant %s "
            "(incident=%s, is_anomaly=%s)",
            customer_id,
            getattr(incident, "id", "unknown"),
            is_anomaly,
        )
        return

    logger.debug(
        "No sequence or embedding on incident %s — skipping online learner feedback",
        getattr(incident, "id", "unknown"),
    )


@router.post(
    "/mark-anomaly",
    response_model=MarkAnomalyIntegratedResponse,
    status_code=status.HTTP_200_OK,
)
async def mark_anomaly_integrated(
    request: MarkAnomalyIntegratedRequest,
    db: AsyncSession = Depends(get_db),
    auth_tenant: str | None = Depends(require_api_key),
) -> MarkAnomalyIntegratedResponse:
    """Operator feedback → active learning → optional auto-retrain.

    Uses ``RetrainingOrchestrator.observe()`` which delegates to
    ``ActiveLearner.collect_operator_feedback()`` — retraining is triggered
    automatically when criteria are met (no separate ``trigger_retraining()``).
    """
    enforce_tenant_access(auth_tenant, request.customer_id)

    stmt = select(Incident).where(Incident.id == request.incident_id)
    result = await db.execute(stmt)
    incident = result.scalar_one_or_none()

    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found",
        )

    if incident.tenant_id != request.customer_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Incident does not belong to this customer",
        )

    orch = await get_orchestrator(request.customer_id, db)

    sequence = incident.sequence
    details = incident.root_cause_details or {}
    detector_scores = details.get("detector_scores")

    feedback_result = orch.observe(
        incident_id=request.incident_id,
        is_true_anomaly=request.is_true_anomaly,
        operator_id=request.operator_id,
        pattern_type_observed=request.pattern_type_observed,
        actual_root_cause=request.actual_root_cause,
        operator_confidence=request.operator_confidence,
        sequence=sequence,
        detector_scores=detector_scores,
    )
    await save_orchestrator(request.customer_id, orch, db)

    # Feed the human-confirmed label to the online learner so it adapts to
    # confirmed anomalies and confirmed normal sequences independently.
    try:
        _observe_online_learner_from_feedback(
            customer_id=request.customer_id,
            incident=incident,
            is_anomaly=request.is_true_anomaly,
        )
    except Exception:
        logger.exception("Online learner feedback observe failed (non-critical)")

    # Phase C: make confirmed anomaly resolution observable in logs so
    # operators can correlate with suggest_fixes queries.
    if request.is_true_anomaly:
        logger.info(
            "Incident %s confirmed as anomaly by operator %s; "
            "resolution triggers suggest_fixes query",
            getattr(incident, "id", "?"),
            request.operator_id,
        )

    tags = list(incident.tags or [])
    tag = CONFIRMED_TAG if request.is_true_anomaly else FALSE_POSITIVE_TAG
    if tag not in tags:
        tags.append(tag)
    incident.tags = tags
    incident.resolved = True
    incident.resolved_at = datetime.now(timezone.utc)
    await db.commit()

    metrics = get_anomaly_metrics()
    if request.is_true_anomaly:
        metrics.record_true_positive(request.incident_id)
    else:
        metrics.record_false_positive(request.incident_id)

    high_quality = feedback_result["high_quality_count"]
    progress = f"{high_quality}/{ACTIVE_LEARNING_MIN_QUALITY_LABELS}"

    if feedback_result["learning_triggered"]:
        return MarkAnomalyIntegratedResponse(
            status="feedback_recorded_retraining_triggered",
            incident_id=request.incident_id,
            learning_triggered=True,
            high_quality_feedback_count=high_quality,
            progress_to_retrain=progress,
            retraining=feedback_result.get("retraining"),
        )

    return MarkAnomalyIntegratedResponse(
        status="feedback_recorded",
        incident_id=request.incident_id,
        learning_triggered=False,
        high_quality_feedback_count=high_quality,
        progress_to_retrain=progress,
        retraining=None,
    )

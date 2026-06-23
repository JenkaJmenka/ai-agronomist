"""
Pydantic v2 schemas for integrated feedback (DB-backed incident wiring).
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class MarkAnomalyIntegratedRequest(BaseModel):
    """Request body for POST /api/feedback/mark-anomaly."""

    incident_id: str = Field(..., min_length=1)
    is_true_anomaly: bool
    operator_id: str = Field(..., min_length=1)
    customer_id: str = Field(..., min_length=1)
    pattern_type_observed: Optional[str] = None
    actual_root_cause: Optional[str] = None
    operator_confidence: float = Field(0.8, ge=0.0, le=1.0)


class MarkAnomalyIntegratedResponse(BaseModel):
    """Response after integrated feedback is recorded."""

    status: str
    incident_id: str
    learning_triggered: bool
    high_quality_feedback_count: int = Field(..., ge=0)
    progress_to_retrain: str
    retraining: Optional[Dict[str, Any]] = None

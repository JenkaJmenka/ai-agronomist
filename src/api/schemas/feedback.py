"""
Pydantic v2 schemas for the active learning / feedback API.

These DTOs validate all external input at the API boundary and shape
the responses of the feedback and learning-progress endpoints.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class FeedbackMarkRequest(BaseModel):
    """Request body for POST /api/feedback/mark."""

    incident_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the incident being annotated",
    )
    is_true_anomaly: bool = Field(
        ..., description="Operator verdict: true anomaly or false positive"
    )
    operator_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the annotating operator",
    )
    customer_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the customer / tenant",
    )
    pattern_type_observed: Optional[str] = Field(
        None, description="Pattern type the operator identified"
    )
    actual_root_cause: Optional[str] = Field(
        None, description="Root cause the operator believes is correct"
    )
    operator_confidence: float = Field(
        0.8,
        ge=0.0,
        le=1.0,
        description="Operator self-assessed confidence [0, 1]",
    )
    severity: Optional[int] = Field(
        None,
        ge=1,
        le=10,
        description="Severity on a 1-10 scale (optional)",
    )
    notes: Optional[str] = Field(
        None, max_length=1024, description="Free-text notes"
    )


class FeedbackMarkResponse(BaseModel):
    """Response body for POST /api/feedback/mark."""

    status: str = Field(..., description="Processing status")
    incident_id: str = Field(..., description="Annotated incident ID")
    learning_triggered: bool = Field(
        ..., description="Whether retraining was triggered"
    )
    feedback_count: int = Field(
        ..., ge=0, description="Total feedback collected"
    )
    high_quality_count: int = Field(
        ..., ge=0, description="High-quality feedback count"
    )
    retraining: Optional[Dict[str, Any]] = Field(
        None, description="Retraining result, if triggered"
    )


class RecordOutcomeRequest(BaseModel):
    """Request body for POST /api/feedback/record-outcome."""

    operator_id: str = Field(
        ...,
        min_length=1,
        description="Operator whose past feedback is being evaluated",
    )
    feedback_was_correct: bool = Field(
        ...,
        description="Whether the operator's feedback matched ground truth",
    )
    customer_id: str = Field(
        ...,
        min_length=1,
        description="Customer context for the operator",
    )


class RecordOutcomeResponse(BaseModel):
    """Response body for POST /api/feedback/record-outcome."""

    status: str = Field(..., description="Processing status")
    operator_id: str = Field(..., description="Updated operator ID")
    new_reliability: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Updated reliability score",
    )


class OperatorReliability(BaseModel):
    """Per-operator reliability breakdown."""

    operator_id: str = Field(..., description="Operator identifier")
    reliability: float = Field(
        ..., ge=0.0, le=1.0, description="Current reliability score"
    )


class StatisticsResponse(BaseModel):
    """Response body for GET /api/feedback/statistics/{tenant_id}."""

    customer_id: str = Field(..., description="Customer identifier")
    total_feedback: int = Field(
        ..., ge=0, description="Total feedback entries"
    )
    high_quality_feedback: int = Field(
        ..., ge=0, description="High-quality feedback entries"
    )
    operator_count: int = Field(
        ..., ge=0, description="Distinct operators who gave feedback"
    )
    retrainings_completed: int = Field(
        ..., ge=0, description="Number of completed retraining cycles"
    )
    last_retrain: Optional[str] = Field(
        None, description="ISO timestamp of last retrain"
    )
    operator_reliability: Dict[str, float] = Field(
        default_factory=dict,
        description="Per-operator reliability scores",
    )


class LearningProgressResponse(BaseModel):
    """Response body for GET /api/feedback/learning-progress/{tenant_id}."""

    customer_id: str = Field(..., description="Customer identifier")
    progress_percent: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="Percent progress toward next retrain",
    )
    samples_needed: int = Field(
        ...,
        ge=0,
        description="High-quality samples still needed",
    )
    high_quality_count: int = Field(
        ..., ge=0, description="Current high-quality count"
    )
    total_feedback: int = Field(
        ..., ge=0, description="Total feedback collected"
    )
    last_retrain: Optional[str] = Field(
        default=None,
        description="ISO timestamp of last retrain; null until the first retraining cycle completes",
    )
    next_retrain_eligible: Optional[datetime] = Field(
        default=None,
        description="Null until the first retraining cycle completes",
    )

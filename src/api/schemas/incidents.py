"""
Pydantic v2 schemas for the incident memory API (Gap 2).

These DTOs validate all external input at the API boundary and shape the
responses of the semantic incident-search endpoints. Embedding dimension is
enforced against LATENT_DIM from the model configuration.
"""

from datetime import datetime
from typing import Any, ClassVar, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.models.fault_classifier import FaultType as _FaultType


class IncidentCreateRequest(BaseModel):
    """Request to store a detected anomaly as an incident."""
    tenant_id: str = Field(
        ..., min_length=1,
        description="Identifier of the multi-tenant customer entity")
    greenhouse_id: str = Field(
        "default", description="Identifier of the greenhouse or sensor zone")
    embedding: List[float] = Field(
        ..., description="Latent-space embedding (encoder mu), length = LATENT_DIM")
    anomaly_score: float = Field(
        ..., ge=0.0, description="Reconstruction error score from the LSTM-VAE")
    sequence: List[List[float]] = Field(
        ..., description="Sensor readings that triggered the anomaly [seq_len, input_dim]")
    embedding_uncertainty: float = Field(
        ..., ge=0.0,
        description="Std dev in latent space (mean of exp(0.5*logvar))")
    root_cause: Optional[str] = Field(
        None, max_length=256, description="Optional root cause label if already known")
    fault_type: Optional[str] = Field(
        None, max_length=32,
        description="Research fault taxonomy: calibration_drift, bias_offset, stuck_fault, accuracy_decline, spike_fault")

    # Auto-derived from the FaultType enum; extended with 'polling_dropout'
    # which will be added to the enum by the ml-engineer (SCADA Phase A).
    # Until the enum is updated, we explicitly include it here so the API
    # can accept and store polling_dropout incidents immediately.
    _VALID_FAULT_TYPES: ClassVar[frozenset[str]] = (
        frozenset(ft.value for ft in _FaultType) | {"polling_dropout"}
    )

    @field_validator('fault_type')
    @classmethod
    def validate_fault_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in cls._VALID_FAULT_TYPES:
            raise ValueError(
                f"Invalid fault_type '{v}'. Must be one of: {sorted(cls._VALID_FAULT_TYPES)}"
            )
        return v

    tags: List[str] = Field(
        default_factory=list, description="User-assigned tags for filtering")
    notes: Optional[str] = Field(
        None, max_length=1024, description="Operator notes/observations")

    @field_validator("embedding")
    @classmethod
    def validate_embedding_dimension(cls, value: List[float]) -> List[float]:
        """Enforce the model latent dimension on incoming embeddings."""
        # Dynamic import keeps the schema layer decoupled from config at import time.
        from src.config import LATENT_DIM

        if len(value) != LATENT_DIM:
            raise ValueError(
                f"Embedding has {len(value)} dimensions, expected {LATENT_DIM}"
            )
        return value


class IncidentStoreResponse(BaseModel):
    """Acknowledgement returned after storing an incident."""
    incident_id: str = Field(..., description="UUID of the stored incident")
    status: str = Field(..., description="Storage status indicator")


class IncidentResponse(BaseModel):
    """A single incident, optionally annotated with its similarity distance."""
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Unique incident UUID")
    tenant_id: str = Field(..., description="Owning tenant identifier")
    greenhouse_id: str = Field(..., description="Greenhouse/zone identifier")
    detected_at: datetime = Field(..., description="When the anomaly was detected")
    anomaly_score: float = Field(..., description="Reconstruction error score")
    embedding_uncertainty: float = Field(
        ..., description="Std dev in latent space at detection time")
    root_cause: Optional[str] = Field(None, description="Root cause label, if known")
    fault_type: Optional[str] = Field(
        None, description="Research fault taxonomy label, if classified")
    root_cause_confidence: Optional[float] = Field(
        None, description="Confidence in the root cause diagnosis (0-1)")
    resolved: bool = Field(..., description="Whether the incident is resolved")
    resolved_at: Optional[datetime] = Field(None, description="Resolution timestamp")
    actions_taken: Optional[List[str]] = Field(
        None, description="Actions performed to fix the incident")
    actions_effective: Optional[bool] = Field(
        None, description="Whether the actions fixed the problem")
    time_to_resolve: Optional[int] = Field(
        None, description="Minutes from detection to resolution")
    tags: Optional[List[str]] = Field(None, description="User-assigned tags")
    notes: Optional[str] = Field(None, description="Operator notes")
    root_cause_details: Optional[Dict[str, Any]] = Field(
        None, description="Diagnosis snapshot (attribution, detector_scores, etc.)"
    )
    sequence: Optional[List[List[float]]] = Field(
        None, description="Sensor sequence at detection time"
    )
    distance: Optional[float] = Field(
        None, description="Cosine distance to the query embedding (lower = more similar)")


class IncidentResolveRequest(BaseModel):
    """Request to mark an incident as resolved."""
    tenant_id: str = Field(..., min_length=1, description="Owning tenant identifier")
    root_cause: str = Field(
        ..., min_length=1, max_length=256, description="Determined root cause label")
    actions_taken: List[str] = Field(
        ..., min_length=1, description="Actions performed to resolve the incident")
    actions_effective: bool = Field(
        ..., description="Whether the actions fixed the problem")
    notes: Optional[str] = Field(None, max_length=1024, description="Operator notes")


class RootCauseSuggestion(BaseModel):
    """A candidate root cause mined from similar resolved incidents."""
    root_cause: str = Field(..., description="Root cause label")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Frequency among similar incidents (0-1)")
    count: int = Field(..., ge=1, description="Times seen among similar incidents")


class ActionSuggestion(BaseModel):
    """A recommended action with its observed effectiveness rate."""
    action: str = Field(..., description="Action label, e.g. 'restart_heating'")
    effectiveness: float = Field(
        ..., ge=0.0, le=1.0, description="Observed effectiveness rate (0-1)")
    count: int = Field(..., ge=1, description="Times the action was applied")


class SuggestionsResponse(BaseModel):
    """Suggested root cause and fixes derived from incident history."""
    most_likely_cause: Optional[RootCauseSuggestion] = Field(
        None, description="Highest-ranked root cause, if any history exists")
    all_causes: List[RootCauseSuggestion] = Field(
        default_factory=list, description="All candidate causes ranked by frequency")
    recommended_actions: List[ActionSuggestion] = Field(
        default_factory=list, description="Actions ranked by effectiveness rate")
    similar_incidents: List[IncidentResponse] = Field(
        default_factory=list, description="The similar incidents the suggestions are based on")
    confidence: int = Field(
        ..., ge=0, description="Number of similar resolved cases the suggestion is based on")
    warning: Optional[str] = Field(
        None, description="Set when no usable history was found")


class RootCauseCount(BaseModel):
    """Aggregated occurrence count for one root cause."""
    root_cause: str = Field(..., description="Root cause label")
    count: int = Field(..., ge=1, description="Number of incidents with this cause")


class StatisticsResponse(BaseModel):
    """Tenant-scoped incident statistics."""
    total_incidents: int = Field(..., ge=0, description="Total stored incidents")
    resolved_incidents: int = Field(..., ge=0, description="Resolved incidents")
    resolved_rate: float = Field(..., ge=0.0, le=1.0, description="Resolved fraction (0-1)")
    avg_time_to_resolve_minutes: Optional[float] = Field(
        None, description="Mean minutes from detection to resolution")
    top_root_causes: List[RootCauseCount] = Field(
        default_factory=list, description="Most frequent root causes")


class RecurringIssue(BaseModel):
    """A root cause that keeps re-occurring for a tenant."""
    root_cause: str = Field(..., description="Root cause label")
    occurrences: int = Field(..., ge=1, description="Number of occurrences")
    last_occurrence: datetime = Field(..., description="Most recent detection time")
    avg_time_to_resolve_minutes: Optional[float] = Field(
        None, description="Mean minutes to resolve this cause")

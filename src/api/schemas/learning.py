"""
Pydantic v2 schemas for the online-learning API (Gap 3).

These DTOs validate all external input at the API boundary for the
streaming fine-tuning endpoints. Sequence shape is enforced against
SEQ_LEN x INPUT_DIM and request batch size against MAX_OBSERVE_BATCH,
all from the model configuration.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_sequence_shape(sequence: List[List[float]], label: str) -> None:
    """Enforce a SEQ_LEN x INPUT_DIM shape on one incoming sequence."""
    # Dynamic import keeps the schema layer decoupled from config at import time.
    from src.config import INPUT_DIM, SEQ_LEN

    if len(sequence) != SEQ_LEN:
        raise ValueError(
            f"{label} has {len(sequence)} timesteps, expected {SEQ_LEN}"
        )
    for step_index, timestep in enumerate(sequence):
        if len(timestep) != INPUT_DIM:
            raise ValueError(
                f"{label}[{step_index}] has {len(timestep)} features, "
                f"expected {INPUT_DIM}"
            )


class ObserveRequest(BaseModel):
    """Request to feed a batch of sensor sequences to a tenant's learner."""
    tenant_id: str = Field(
        ..., min_length=1,
        description="Identifier of the multi-tenant customer entity")
    greenhouse_id: str = Field(
        "default", description="Identifier of the greenhouse or sensor zone")
    sequences: List[List[List[float]]] = Field(
        ..., min_length=1,
        description="Batch of sensor sequences, each [SEQ_LEN, INPUT_DIM]")

    @field_validator("sequences")
    @classmethod
    def validate_sequences(
        cls, value: List[List[List[float]]]
    ) -> List[List[List[float]]]:
        """Cap the batch size and enforce SEQ_LEN x INPUT_DIM per sequence."""
        from src.config import MAX_OBSERVE_BATCH

        if len(value) > MAX_OBSERVE_BATCH:
            raise ValueError(
                f"Batch of {len(value)} sequences exceeds the maximum of "
                f"{MAX_OBSERVE_BATCH}"
            )
        for seq_index, sequence in enumerate(value):
            _validate_sequence_shape(sequence, f"sequences[{seq_index}]")
        return value


class DriftStatus(BaseModel):
    """Drift evaluation produced by the DriftDetector for one loss stream."""
    drift_detected: bool = Field(
        ..., description="Whether the recent loss window indicates drift")
    drift_score: float = Field(
        ..., description="Normalized recent-vs-historical loss increase")
    recent_avg: Optional[float] = Field(
        None, description="Mean loss over the recent window")
    historical_avg: Optional[float] = Field(
        None, description="Mean loss over the historical baseline")
    action: Optional[str] = Field(
        None, description="'retrain_model' or 'continue_learning'")
    warning: Optional[str] = Field(
        None, description="Set while the detector lacks enough history")


class ObserveResponse(BaseModel):
    """Outcome of observing a batch of sequences."""
    losses: List[float] = Field(
        ..., description="Per-sequence VAE loss (anomaly signal)")
    sequences_observed: int = Field(
        ..., ge=0, description="Number of sequences processed in this batch")
    updates_performed: int = Field(
        ..., ge=0, description="Online SGD updates triggered by this batch")
    drift: Optional[DriftStatus] = Field(
        None, description="Latest drift evaluation after this batch")


class PredictRequest(BaseModel):
    """Request to score a single sequence with a tenant's adapted model."""
    tenant_id: str = Field(
        ..., min_length=1,
        description="Identifier of the multi-tenant customer entity")
    sequence: List[List[float]] = Field(
        ..., description="One sensor sequence of shape [SEQ_LEN, INPUT_DIM]")

    @field_validator("sequence")
    @classmethod
    def validate_sequence(cls, value: List[List[float]]) -> List[List[float]]:
        """Enforce the SEQ_LEN x INPUT_DIM shape on the incoming sequence."""
        _validate_sequence_shape(value, "sequence")
        return value


class PredictResponse(BaseModel):
    """Prediction from a tenant's online-adapted (or base) model."""
    anomaly_score: float = Field(
        ..., description="Reconstruction MSE of the sequence")
    embedding: List[float] = Field(
        ..., description="Latent-space embedding (encoder mu), length = LATENT_DIM")
    uncertainty: float = Field(
        ..., description="Mean latent std (exp(0.5*logvar))")
    trend: Optional[str] = Field(
        None, description="'increasing'/'stable' once enough losses are seen")
    updates_performed: int = Field(
        0, ge=0, description="Total online updates applied to this tenant's model")
    learner_active: bool = Field(
        ..., description="False when the frozen base model produced the prediction")


class LearningSummaryResponse(BaseModel):
    """Aggregated learning-log summary for a tenant."""
    events_by_type: Dict[str, int] = Field(
        default_factory=dict, description="Logged event counts per event_type")
    total_events: int = Field(..., ge=0, description="Total logged events")
    total_updates_performed: int = Field(
        ..., ge=0, description="Sum of update steps across all events")
    last_event_at: Optional[datetime] = Field(
        None, description="Timestamp of the most recent event")


class LearnerStatisticsResponse(BaseModel):
    """Live learner statistics merged with the persisted learning summary."""
    tenant_id: str = Field(..., description="Owning tenant identifier")
    learner: Dict[str, Any] = Field(
        ..., description="In-memory learner statistics, or {'status': 'no_learner'}")
    summary: LearningSummaryResponse = Field(
        ..., description="Aggregated summary from the learning-log store")


class LearningHistoryEntry(BaseModel):
    """One persisted learning event (LearningLog row)."""
    # protected_namespaces=() allows the model_checkpoint_path column name.
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: str = Field(..., description="Unique learning-event UUID")
    tenant_id: str = Field(..., description="Owning tenant identifier")
    greenhouse_id: str = Field(..., description="Greenhouse/zone identifier")
    event_type: str = Field(..., description="Learning event kind")
    created_at: datetime = Field(..., description="When the event occurred")
    sequences_observed: int = Field(
        ..., ge=0, description="Sequences observed in this event")
    updates_performed: int = Field(
        ..., ge=0, description="Update steps performed in this event")
    loss_before: Optional[float] = Field(
        None, description="Loss before the update step")
    loss_after: Optional[float] = Field(
        None, description="Loss after the update step")
    drift_score: Optional[float] = Field(
        None, description="Computed drift score, for drift_check events")
    drift_detected: Optional[bool] = Field(
        None, description="Whether the drift check flagged drift")
    details: Optional[Dict[str, Any]] = Field(
        None, description="Free-form structured event payload")
    model_checkpoint_path: Optional[str] = Field(
        None, description="Checkpoint path, for checkpoint_saved events")


class DriftStateResponse(BaseModel):
    """Latest persisted drift check plus live detector state for a tenant."""
    tenant_id: str = Field(..., description="Owning tenant identifier")
    latest_check: Optional[LearningHistoryEntry] = Field(
        None, description="Most recent persisted drift_check event")
    detector: Optional[Dict[str, Any]] = Field(
        None, description="Live DriftDetector state, when a learner exists")


class ActiveModelVersionResponse(BaseModel):
    """Active model deployment for a tenant."""
    model_id: str = Field(..., description="Identifier of the active model version")
    checkpoint_path: str = Field(..., description="Path to the model checkpoint file")

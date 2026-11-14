"""
Pydantic v2 schemas for the integrated comprehensive inference pipeline.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from src.config import INPUT_DIM, SEQ_LEN


class SensorPairSequence(BaseModel):
    """A pair of redundant co-located sensor time series."""

    variable: str = Field(..., description="Physical variable (e.g. 'temperature')")
    sensor_a: List[float] = Field(..., description="Primary sensor readings")
    sensor_b: List[float] = Field(..., description="Redundant sensor readings")


class ComprehensiveInferenceRequest(BaseModel):
    """Request body for POST /api/inference/comprehensive."""

    customer_id: str = Field(..., min_length=1, description="Tenant / customer ID")
    greenhouse_id: str = Field("default", description="Greenhouse or zone ID")
    sequence: List[List[Optional[float]]] = Field(
        ...,
        description=(
            "Sensor sequence [seq_len, input_dim].  JSON null is accepted for any "
            "sensor value and treated as a missing sample (NaN) before the pipeline — "
            "this is the expected representation for polling-dropout gaps.  NaN is also "
            "accepted (Pydantic coerces it from the JSON number token)."
        ),
    )
    baseline_sequences: Optional[List[List[List[float]]]] = Field(
        None,
        description="Optional normal baseline sequences for pattern/attribution",
    )
    sensor_pairs: Optional[List[SensorPairSequence]] = Field(
        None,
        description="Optional redundant sensor pairs for cross-validation",
    )
    sequence_timestamp: Optional[str] = Field(
        None,
        description="ISO-8601 timestamp of the sequence for diurnal profile checks",
    )
    climate_context: Optional[Dict[str, float]] = Field(
        None,
        description=(
            "Optional climate-computer / microclimate scalars for setpoint-aware "
            "SCADA rules (t_setpoint, t_plant, t_outdoor, i_solar, …)"
        ),
    )

    @field_validator("sequence")
    @classmethod
    def validate_sequence_shape(
        cls, value: List[List[Optional[float]]]
    ) -> List[List[float]]:
        """Validate shape and convert JSON null / Python None to NaN.

        A null sensor value represents a missing sample (polling dropout).
        NaN is preserved unchanged.  Any other non-float value raises a
        ValueError.  Shape must be (SEQ_LEN, INPUT_DIM).
        """
        if len(value) != SEQ_LEN:
            raise ValueError(f"sequence must have {SEQ_LEN} timesteps, got {len(value)}")
        result: list[list[float]] = []
        for row in value:
            if len(row) != INPUT_DIM:
                raise ValueError(
                    f"each timestep must have {INPUT_DIM} features, got {len(row)}"
                )
            # None (JSON null) → NaN; existing NaN passes through; other floats
            # pass through unchanged.  This is the only place the conversion
            # happens so downstream code (np.asarray, the pipeline) always
            # receives a plain float, never None.
            result.append([float("nan") if v is None else float(v) for v in row])
        return result


class LearningProgressSummary(BaseModel):
    """Learning progress embedded in comprehensive inference responses."""

    high_quality_feedback: int = Field(..., ge=0)
    needed_for_retrain: int = Field(..., ge=0)
    retrainings_completed: int = Field(..., ge=0)


class ComprehensiveInferenceResponse(BaseModel):
    """Full pipeline result returned to API consumers."""

    is_anomaly: bool
    anomaly_score: float
    confidence: float
    confidence_level: str
    pattern_type: str
    attribution: Dict[str, float]
    affected_sensors: List[str]
    pattern_scores: Dict[str, float]
    root_cause: str
    root_cause_confidence: float
    reasoning: str
    alternative_causes: List[str]
    severity: str
    urgency: str
    immediate_actions: List[str]
    follow_up_actions: List[str]
    all_recommendations: List[str]
    requires_technician: bool
    estimated_resolution_minutes: int
    incident_id: Optional[str] = None
    detector_scores: Optional[Dict[str, float]] = None
    ensemble_score: Optional[float] = None
    ensemble_agreement: Optional[int] = None
    learning_progress: Optional[LearningProgressSummary] = None
    timestamp: str
    pipeline_version: str = "1.0"
    scada_anomalies: Optional[List[Dict[str, Any]]] = None


class BatchInferenceRequest(BaseModel):
    """Request body for POST /api/inference/batch."""

    customer_id: str = Field(..., min_length=1)
    greenhouse_id: str = Field("default")
    sequences: List[List[List[Optional[float]]]] = Field(
        ...,
        min_length=1,
        description=(
            "List of sensor sequences.  JSON null is accepted for any sensor value "
            "and treated as a missing sample (NaN) before the pipeline."
        ),
    )

    @field_validator("sequences")
    @classmethod
    def convert_null_to_nan(
        cls, value: List[List[List[Optional[float]]]]
    ) -> List[List[List[float]]]:
        """Convert JSON null / Python None to NaN in every sequence.

        None (JSON null) represents a missing sample (polling dropout).
        NaN is preserved.  Shape validation is left to the pipeline.
        """
        return [
            [
                [float("nan") if v is None else float(v) for v in row]
                for row in seq
            ]
            for seq in value
        ]


class BatchInferenceResponse(BaseModel):
    """Aggregated batch inference result."""

    batch_size: int
    anomalies_detected: int
    avg_confidence: float
    results: List[ComprehensiveInferenceResponse]


class MetricsResponse(BaseModel):
    """Response for GET /api/monitoring/metrics."""

    period_hours: int
    total_detections: int
    false_positives: int
    true_positives: int
    false_positive_rate: float
    avg_confidence: float
    pattern_distribution: Dict[str, int]
    confidence_distribution: Dict[str, int]

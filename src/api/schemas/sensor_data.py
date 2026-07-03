"""
Pydantic v2 schemas for the AI Agronomist sensor data API.

This module defines the data transfer objects (DTOs) for greenhouse sensor readings
ingestion, CSV uploading, validation, and real-time machine learning inference.
These schemas act as a strict validation layer between external requests, the
FastAPI routing layer, and the database ORM, ensuring loose coupling between the
underlying machine learning models and API delivery.
"""

from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator


# =============================================================================
# Sensor Reading Schemas
# =============================================================================

class SensorReadingCreate(BaseModel):
    """
    Schema for creating a single sensor reading record.
    Used for bulk sensor data uploads or real-time measurements streaming.
    """
    timestamp: datetime = Field(
        ...,
        description="Timestamp of the sensor reading in ISO 8601 format"
    )
    temperature: float = Field(
        ...,
        ge=0.0,
        le=50.0,
        description="Greenhouse ambient temperature in degrees Celsius (Allowed range: 0.0 to 50.0)"
    )
    humidity: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="Relative humidity percentage (Allowed range: 0.0 to 100.0)"
    )
    co2: float = Field(
        ...,
        ge=0.0,
        le=1000.0,
        description="Carbon dioxide concentration in parts per million (ppm) (Allowed range: 0.0 to 1000.0)"
    )
    light: float = Field(
        ...,
        ge=0.0,
        le=2000.0,
        description="Light intensity/level in lux (Allowed range: 0.0 to 2000.0)"
    )


class SensorReadingResponse(BaseModel):
    """
    Schema for a sensor reading response from queries.
    Maps directly to the database ORM schema.
    """
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(
        ...,
        description="Unique database identifier for python-generated record"
    )
    tenant_id: str = Field(
        ...,
        description="Customer/Tenant identifier for multi-tenant isolation"
    )
    greenhouse_id: str = Field(
        ...,
        description="Greenhouse/Zone identifier within the customer facility"
    )
    timestamp: datetime = Field(
        ...,
        description="Timestamp of the sensor reading"
    )
    temperature: float = Field(
        ...,
        description="Ambient temperature in degrees Celsius"
    )
    humidity: float = Field(
        ...,
        description="Relative humidity percentage"
    )
    co2: float = Field(
        ...,
        description="Carbon dioxide level in ppm"
    )
    light: float = Field(
        ...,
        description="Light intensity in lux"
    )
    anomaly_score: Optional[float] = Field(
        None,
        description="Calculated model anomaly reconstruction score or probability"
    )
    created_at: datetime = Field(
        ...,
        description="Timestamp indicating when the record was persisted in the database"
    )


# =============================================================================
# CSV / JSON Bulk Upload Schemas
# =============================================================================

class UploadRequest(BaseModel):
    """
    Schema for uploading sensor data metadata/payload.
    Can be used for bulk JSON ingestion or as a descriptor for file uploads.
    """
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the tenant (owner of the data)"
    )
    greenhouse_id: str = Field(
        "default",
        description="Identifier of the greenhouse facility"
    )
    readings: Optional[List[SensorReadingCreate]] = Field(
        None,
        description="Optional list of sensor readings for direct bulk JSON ingestion"
    )


class SensorRange(BaseModel):
    """
    Represents the physical limits (minimum and maximum values)
    observed for a specific sensor variable within an uploaded dataset.
    """
    min: float = Field(
        ...,
        description="Minimum value seen in the dataset slice"
    )
    max: float = Field(
        ...,
        description="Maximum value seen in the dataset slice"
    )


class UploadResponse(BaseModel):
    """
    Response schema for sensor data uploads, presenting statistics
    regarding ingestion performance, parsed ranges, and target identification.
    """
    uploaded: int = Field(
        ...,
        ge=0,
        description="Number of sensor records successfully inserted into the database"
    )
    errors: int = Field(
        ...,
        ge=0,
        description="Number of records that failed validation or processing constraints"
    )
    skipped: int = Field(
        ...,
        ge=0,
        description="Number of records skipped during insertion (e.g. duplicate rows)"
    )
    ranges: Dict[str, SensorRange] = Field(
        ...,
        description="Dictionary mapping each sensor key (temperature, humidity, co2, light) to its observed range"
    )
    tenant_id: str = Field(
        ...,
        description="Identifier of the tenant associated with the upload"
    )
    greenhouse_id: str = Field(
        ...,
        description="Identifier of the greenhouse associated with the upload"
    )
    message: Optional[str] = Field(
        None,
        description="Optional detailed informational or warning status message from the upload processing"
    )


# =============================================================================
# ML Real-time Inference / Predictor Schemas
# =============================================================================

class InferenceRequest(BaseModel):
    """
    Schema for requesting real-time model predictions/anomaly scoring on sequence blocks.
    Ensures input matrices meet physical and shape constraints for the VAE model.
    """
    tenant_id: str = Field(
        ...,
        min_length=1,
        description="Identifier of the tenant requesting model predictions"
    )
    greenhouse_id: str = Field(
        "default",
        description="Identifier of the greenhouse"
    )
    sequences: List[List[List[float]]] = Field(
        ...,
        description="Batch of temporal sequences of shape (batch, seq_len=20, input_dim=4). "
                    "Features must be ordered exactly as: [temperature, humidity, CO2, light]"
    )
    timestamps: Optional[List[str]] = Field(
        None,
        description="One ISO-8601 timestamp per sequence (optional for pure inference). "
                    "Required when persist_scores=True — must align 1:1 with sequences. "
                    "The field stays Optional for wire compatibility; the route enforces "
                    "presence when persistence is requested.",
    )
    persist_scores: bool = Field(
        default=False,
        description="When True, anomaly scores are written back to the matching SensorReading "
                    "rows in the database.  Requires timestamps to be provided with one entry "
                    "per sequence; the route returns HTTP 400 if timestamps are absent or the "
                    "count does not match len(sequences).",
    )

    @field_validator("sequences")
    @classmethod
    def validate_tensor_dimensions(cls, val: List[List[List[float]]]) -> List[List[List[float]]]:
        """
        Validates that the input sequences match the expected shape constraints
        defined in the model configuration.
        """
        # Dynamic import to avoid circular dependencies and ensure config-driven constraints
        from src.config import SEQ_LEN, INPUT_DIM

        for seq_idx, sequence in enumerate(val):
            if len(sequence) != SEQ_LEN:
                raise ValueError(
                    f"Sequence at batch index {seq_idx} has length {len(sequence)}, "
                    f"but model architecture requires a exact sequence length of {SEQ_LEN}."
                )
            for step_idx, step_features in enumerate(sequence):
                if len(step_features) != INPUT_DIM:
                    raise ValueError(
                        f"Sequence {seq_idx} at timestep {step_idx} has {len(step_features)} features, "
                        f"but model architecture requires exact feature count/input dimension of {INPUT_DIM}."
                    )
        return val


class HighAnomalyDetail(BaseModel):
    """
    Structure detailing specific sequence events that exhibit severe
    anomaly levels above configured threshold boundaries.
    """
    sequence_index: int = Field(
        ...,
        ge=0,
        description="Index placement of the sequence in the sent batch list"
    )
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="High anomaly representation reconstruction error score [0, 1]"
    )
    timestamp: datetime = Field(
        ...,
        description="Timestamp coordinate associated with the anomalous incident"
    )


class InferenceResponse(BaseModel):
    """
    Schema representing results returned from model inference.
    Contains overall performance counters, a continuous array of scores, and high anomalies indicators.
    """
    tenant_id: str = Field(
        ...,
        description="Tenant/Owner identification tag linked to inference session"
    )
    greenhouse_id: str = Field(
        ...,
        description="Greenhouse identification tag linked to inference session"
    )
    sequences_processed: int = Field(
        ...,
        ge=0,
        description="Number of sequences successfully analyzed by the LSTM-VAE"
    )
    anomalies_detected: int = Field(
        ...,
        ge=0,
        description="Total counts of critical sequences classified as anomalous"
    )
    anomaly_scores: List[float] = Field(
        ...,
        description="Ordered list of computed reconstruction error ratios for each sequence in input batch"
    )
    high_anomalies: List[HighAnomalyDetail] = Field(
        default_factory=list,
        description="Metadata on anomalous segments exceeding warning criteria threshold limits"
    )
    embeddings: Optional[List[List[float]]] = Field(
        None,
        description="Latent-space embeddings (encoder mu) for each sequence in the batch, "
                    "shape (batch, LATENT_DIM). Enables semantic anomaly similarity search."
    )
    uncertainties: Optional[List[float]] = Field(
        None,
        description="Per-sequence model uncertainty: mean of exp(0.5*logvar) (std) across latent dimensions. "
                    "Higher values indicate the model is less confident about the anomaly type."
    )
    embedding_dim: Optional[int] = Field(
        None,
        ge=1,
        description="Dimensionality of each embedding vector (LATENT_DIM from model config)"
    )


class UploadHighAnomalyDetail(BaseModel):
    """Anomaly event from cold-run CSV upload (LSTM or statistical)."""

    score: float = Field(..., ge=0.0, le=1.0)
    timestamp: str
    detector: str = Field(default="lstm", description="lstm, statistical, or lstm+statistical")
    sequence_index: Optional[int] = Field(default=None, ge=0)
    column: Optional[str] = None
    z_score: Optional[float] = None
    severity: Optional[str] = None
    method: Optional[str] = None
    value: Optional[float] = None
    agreement: bool = Field(
        default=False,
        description="True when both LSTM and statistical detectors flagged this timestamp",
    )


class StatisticalAnomalyDetail(BaseModel):
    """Single per-column statistical anomaly from cold-run detection."""

    timestamp: str
    column: str
    value: float
    z_score: float
    severity: str
    method: str


class StatisticalColdRunSummary(BaseModel):
    """Aggregate statistical cold-run results."""

    anomalies: List[StatisticalAnomalyDetail] = Field(default_factory=list)
    baseline_columns: List[str] = Field(default_factory=list)
    total_scored: int = 0


class UploadInferenceResponse(BaseModel):
    """Response from POST /api/inference/upload (cold-run CSV scoring)."""

    tenant_id: str
    greenhouse_id: str
    sequences_processed: int = Field(..., ge=0)
    anomalies_detected: int = Field(..., ge=0)
    anomaly_scores: List[float]
    high_anomalies: List[UploadHighAnomalyDetail] = Field(default_factory=list)
    threshold_used: float = Field(..., ge=0.0, le=1.0)
    threshold_method: str
    imputed_channels: List[str] = Field(default_factory=list)
    statistical_summary: Optional[StatisticalColdRunSummary] = None
    embeddings: List[List[float]] = Field(default_factory=list)
    uncertainties: List[float] = Field(default_factory=list)
    embedding_dim: int = Field(..., ge=1)
    sequence_timestamps: List[str] = Field(
        default_factory=list,
        description="ISO timestamp per sequence (aligned with anomaly_scores)",
    )
    data_persisted: bool = False
    rows_uploaded: int = Field(default=0, ge=0)
    sensor_rows_uploaded: int = Field(default=0, ge=0)
    raw_rows_uploaded: int = Field(default=0, ge=0)

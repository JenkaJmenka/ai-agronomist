"""
SQLAlchemy database models for the AI Agronomist platform.
"""

import datetime
import uuid
from typing import Any, Dict, List, Optional
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.config import LATENT_DIM
from src.db.embedding_type import EmbeddingType


def _generate_uuid() -> str:
    """Portable string UUID primary key (works on SQLite and PostgreSQL)."""
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy database models."""
    pass


class SensorReading(Base):
    """
    Model representing a single sensor telemetry reading.

    This model maps to the 'sensor_readings' table in the database and is
    specifically structured to support high-throughput, multi-tenant greenhouse
    environments with indexing on tenant_id and timestamp.
    """
    __tablename__ = "sensor_readings"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
        doc="Unique incrementing database identifier"
    )
    tenant_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        doc="Identifier for the multi-tenant customer entity"
    )
    greenhouse_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="default",
        doc="Identifier for the specific greenhouse or sensor zone"
    )
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        doc="Measurement timestamp with timezone awareness"
    )
    temperature: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        doc="Greenhouse ambient temperature in degrees Celsius"
    )
    humidity: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        doc="Relative humidity percentage"
    )
    co2: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        doc="CO2 levels in parts per million (ppm)"
    )
    light: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        doc="Light intensity in lux"
    )
    anomaly_score: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        doc="Model inference output score indicating confidence in anomaly classification"
    )
    anomaly_type: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        doc="Synthetic anomaly type tag set by the inject endpoint; None for real sensor data"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        doc="Audit field capturing database record instantiation timestamp"
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "greenhouse_id",
            "timestamp",
            name="uq_sensor_readings_tenant_greenhouse_timestamp",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SensorReading(id={self.id}, tenant_id={self.tenant_id!r}, greenhouse_id={self.greenhouse_id!r}, "
            f"timestamp={self.timestamp.isoformat() if self.timestamp else 'None'}, "
            f"temperature={self.temperature}, humidity={self.humidity}, "
            f"co2={self.co2}, light={self.light}, anomaly_score={self.anomaly_score})>"
        )

class ModelBaseline(Base):
    __tablename__ = "model_baseline"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String, nullable=False)
    greenhouse_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    baseline_json: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())

class ModelVersion(Base):
    __tablename__ = "model_version"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String, nullable=False)
    model_type: Mapped[str] = mapped_column(String, nullable=False)
    checkpoint_path: Mapped[str] = mapped_column(String, nullable=False)
    baseline_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metrics_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())
    status: Mapped[str] = mapped_column(String, default="active")

class ModelDeployment(Base):
    __tablename__ = "model_deployment"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    deployed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())
    is_current: Mapped[bool] = mapped_column(default=False)

class Incident(Base):
    """
    A detected anomaly stored with its latent-space embedding for semantic
    search ("find similar past incidents"), root-cause analysis and
    resolution tracking. Multi-tenant: every query MUST filter tenant_id.
    """
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid,
        doc="Unique incident UUID (string for cross-backend portability)")
    tenant_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True,
        doc="Identifier for the multi-tenant customer entity")
    greenhouse_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default",
        doc="Identifier for the specific greenhouse or sensor zone")

    detected_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
        doc="When the anomaly was first detected")
    resolved_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        doc="When the incident was resolved (null = still open)")
    reported_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        doc="When an operator reported the issue")

    sequence: Mapped[List[Any]] = mapped_column(
        JSON, nullable=False,
        doc="Sensor readings that triggered the anomaly [seq_len, input_dim]")
    sequence_length: Mapped[int] = mapped_column(
        Integer, nullable=False,
        doc="Number of timesteps in the stored sequence")
    anomaly_score: Mapped[float] = mapped_column(
        Float, nullable=False,
        doc="Reconstruction error score from the LSTM-VAE")
    embedding: Mapped[List[float]] = mapped_column(
        EmbeddingType(LATENT_DIM), nullable=False,
        doc="Latent-space embedding (encoder mu); pgvector on PostgreSQL, JSON on SQLite")
    embedding_uncertainty: Mapped[float] = mapped_column(
        Float, nullable=False,
        doc="Std dev in latent space (mean of exp(0.5*logvar)); higher = more uncertain")
    fault_type: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True,
        doc="Research fault taxonomy: calibration_drift, bias_offset, stuck_fault, accuracy_decline, spike_fault")

    root_cause: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True, index=True,
        doc="Inferred root cause label, e.g. 'boiler_failure'")
    root_cause_confidence: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="Confidence in root cause diagnosis (0-1)")
    root_cause_details: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True, doc="Structured diagnosis details")

    resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        doc="Has this incident been resolved?")
    actions_taken: Mapped[Optional[List[str]]] = mapped_column(
        JSON, nullable=True, doc="Actions performed to fix the incident")
    actions_effective: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True, doc="Did the actions fix the problem?")
    effectiveness_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="How quickly it was resolved (0-1, 1 = instant)")

    time_to_detect: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, doc="Minutes from problem onset to detection")
    time_to_resolve: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, doc="Minutes from detection to resolution")
    crop_damage_estimated: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="Estimated crop loss percentage (0-100)")

    tags: Mapped[Optional[List[str]]] = mapped_column(
        JSON, nullable=True, default=list, doc="User-assigned tags for filtering")
    notes: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True, doc="Operator notes/observations")

    __table_args__ = (
        Index("ix_incidents_tenant_detected", "tenant_id", "detected_at"),
        Index("ix_incidents_resolved_detected", "resolved", "detected_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Incident(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"root_cause={self.root_cause!r}, resolved={self.resolved})>"
        )


class IncidentAction(Base):
    """A single action taken while resolving an incident."""
    __tablename__ = "incident_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_generate_uuid)
    incident_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("incidents.id"), nullable=False, index=True)
    action: Mapped[str] = mapped_column(
        String(256), nullable=False, doc="Action performed, e.g. 'restart_heating'")
    action_time: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), server_default=func.now())
    effectiveness: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True, doc="True = fixed, False = did not help, None = unknown")
    notes: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)


class OperatorFeedback(Base):
    __tablename__ = "operator_feedback"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(String, nullable=False)
    sequence_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_false_positive: Mapped[Optional[bool]] = mapped_column(nullable=True)
    feedback_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=func.now(), server_default=func.now())


class LearningLog(Base):
    """
    Audit trail for the online-learning subsystem (Gap 3).

    Each row records one learning event: a batch of observed sequences, an
    incremental update step, a drift check, a checkpoint save or a retrain
    recommendation. Multi-tenant: every query MUST filter tenant_id.
    """
    __tablename__ = "learning_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid,
        doc="Unique learning-event UUID (string for cross-backend portability)")
    tenant_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True,
        doc="Identifier for the multi-tenant customer entity")
    greenhouse_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default",
        doc="Identifier for the specific greenhouse or sensor zone")

    event_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
        doc="Learning event kind: 'observe_batch', 'update_step', "
            "'drift_check', 'checkpoint_saved', 'retrain_recommended'")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=func.now(), server_default=func.now(),
        doc="When the learning event occurred")

    sequences_observed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        doc="Number of sensor sequences observed in this event")
    updates_performed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
        doc="Number of model update steps performed in this event")

    loss_before: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="Model loss before the update step")
    loss_after: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="Model loss after the update step")
    drift_score: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, doc="Computed distribution drift score")
    drift_detected: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True, doc="Did the drift check flag drift?")

    details: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True, doc="Free-form structured payload for the event")
    model_checkpoint_path: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True,
        doc="Path of the checkpoint written by a 'checkpoint_saved' event")

    __table_args__ = (
        Index("ix_learning_logs_tenant_created", "tenant_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<LearningLog(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"event_type={self.event_type!r}, created_at={self.created_at})>"
        )


class LearningState(Base):
    """Persisted active-learning orchestrator state per customer."""

    __tablename__ = "learning_states"

    customer_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        doc="Customer / tenant identifier",
    )
    state_json: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        doc="Serialized RetrainingOrchestrator state",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        doc="Last persistence timestamp (UTC)",
    )


class CustomerModelConfig(Base):
    """Per-customer model tuning overrides (thresholds, ensemble weights)."""

    __tablename__ = "customer_model_config"

    customer_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        doc="Customer / tenant identifier",
    )
    config_json: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        doc="Merged override keys (ANOMALY_THRESHOLD, ENSEMBLE_* etc.)",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        doc="Last update timestamp (UTC)",
    )


class MetricsSnapshot(Base):
    """Point-in-time aggregate snapshot of AnomalyMetrics for a tenant."""

    __tablename__ = "metrics_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    snapshot_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    total_detections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    false_positive_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mean_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pattern_distribution: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    confidence_distribution: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class RawReading(Base):
    """Stores every CSV column as JSON for full-fidelity dashboard display."""

    __tablename__ = "raw_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    greenhouse_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # Which sensor system produced this row (e.g. column name used as timestamp source)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # clean_col_name → float value for all detected numeric sensor columns
    data: Mapped[Dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_raw_readings_tenant_timestamp", "tenant_id", "timestamp"),
        UniqueConstraint(
            "tenant_id",
            "greenhouse_id",
            "timestamp",
            name="uq_raw_readings_tenant_greenhouse_timestamp",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<RawReading(id={self.id}, tenant_id={self.tenant_id!r}, "
            f"greenhouse_id={self.greenhouse_id!r}, timestamp={self.timestamp})>"
        )

"""
SQLAlchemy database models for the AI Agronomist platform.
"""

import datetime
import uuid
from typing import Any, Dict, List, Optional
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, UniqueConstraint, func
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
        doc="Light intensity in W/m² (solar irradiance — canonical unit, see LIGHT_CANONICAL_UNIT)"
    )
    anomaly_score: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        doc="Model inference output score — canonical [0,1] scale (ADR-005). "
            "0.5 == the producing detector's own decision point.",
    )
    anomaly_score_raw: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        doc="Producer's native score before the ADR-005 canonical transform. "
            "Preserved for recalibration without re-inference. NULL until migration 023.",
    )
    score_scale: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        doc="Provenance label for the scale used: live | upload | batch | "
            "synthetic | legacy | unknown. NULL until migration 023.",
    )
    trigger_channel: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        doc="Raw column name with the highest-|z| hit at this timestamp. "
            "Populated by the CSV upload path (statistical detector). NULL until migration 024.",
    )
    trigger_detector: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        doc="Detector name (statistical, lstm, lstm+statistical) for the trigger_channel hit. "
            "NULL until migration 024.",
    )
    detected_severity: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        doc="Severity label (critical, warning) from the triggering detector. "
            "NULL until migration 024.",
    )
    trigger_value: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
        doc="Raw sensor value at trigger_channel that caused the alert. "
            "NULL for LSTM-only rows. NULL until migration 024.",
    )
    anomaly_type: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        doc="Synthetic anomaly type tag set by the inject endpoint; None for real sensor data"
    )
    source: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        doc="Provenance tag: 'live' for rows persisted by the live SCADA pipeline; None for CSV uploads"
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


class Job(Base):
    """Async job record for queue-processed work (uploads, training).

    RabbitMQ is not queryable, so job status/progress/results live here; the
    dashboard polls GET /api/jobs/{job_id}.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_generate_uuid)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    greenhouse_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # upload_csv | training_online | training_retrain
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # pending | running | finalizing | completed | failed | partially_failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    total_portions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    completed_portions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_portions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    result: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )
    finished_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_jobs_tenant_created", "tenant_id", "created_at"),)

    def __repr__(self) -> str:
        return f"<Job(id={self.id!r}, type={self.job_type!r}, status={self.status!r})>"


class JobPortion(Base):
    """Per-portion record: idempotency gate for redeliveries + finalize input."""

    __tablename__ = "job_portions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    portion_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    # completed | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("job_id", "portion_idx", name="uq_job_portions_job_idx"),
    )

    def __repr__(self) -> str:
        return f"<JobPortion(job_id={self.job_id!r}, idx={self.portion_idx}, status={self.status!r})>"


class ModelArtifact(Base):
    """Binary model checkpoints shared across services via the DB.

    Railway containers share no filesystem, so checkpoints produced by the
    worker (retraining, online-learner state) travel as bytea rows addressed
    by a stable key ("db://model_artifacts/<key>").
    """

    __tablename__ = "model_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_generate_uuid)
    key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True, index=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<ModelArtifact(key={self.key!r}, size={self.size_bytes})>"


class OperationalConfig(Base):
    """Per-tenant/greenhouse operational config (crop setpoints, season map, day window).

    Composite primary key ``(tenant_id, greenhouse_id)`` so each tenant can
    maintain separate configs per greenhouse without a surrogate ID column.
    ``config_json`` holds the full canonical SCADA operational document:
    crop, setpoints, day_window, season_map, seasonal_overrides.
    """

    __tablename__ = "operational_config"

    tenant_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        doc="Multi-tenant customer identifier",
    )
    greenhouse_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        default="default",
        doc="Greenhouse zone identifier; defaults to 'default'",
    )
    config_json: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        doc=(
            "SCADA operational config: crop, setpoints{channel:{day:[lo,hi],"
            " night:[lo,hi]|null}}, day_window{start_hour,end_hour},"
            " season_map{'1'..'12':'summer'|'winter'},"
            " seasonal_overrides{season:{detector_scale:float|null}}"
        ),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        doc="Last update timestamp (UTC)",
    )

    def __repr__(self) -> str:
        return (
            f"<OperationalConfig(tenant_id={self.tenant_id!r},"
            f" greenhouse_id={self.greenhouse_id!r},"
            f" crop={self.config_json.get('crop') if self.config_json else None!r})>"
        )


class Greenhouse(Base):
    """Registry of tenant greenhouses — exists independently of sensor readings.

    A greenhouse row is created when a tenant is provisioned for a demo or
    when the operator calls POST /api/readings/greenhouses.  This decouples
    greenhouse existence from data upload: a greenhouse can be registered with
    zero readings so that the FE can render the selector and the live stream
    can report meaningful empty-state status rather than silently showing nothing.
    """

    __tablename__ = "greenhouses"

    tenant_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        doc="Multi-tenant customer identifier",
    )
    greenhouse_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        doc="Greenhouse zone identifier",
    )
    registered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        doc="When this greenhouse was registered",
    )

    def __repr__(self) -> str:
        return (
            f"<Greenhouse(tenant_id={self.tenant_id!r},"
            f" greenhouse_id={self.greenhouse_id!r})>"
        )

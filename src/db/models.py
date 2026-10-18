"""
SQLAlchemy database models for the AI Agronomist platform.
"""

import datetime
import uuid
from typing import Any, ClassVar, Dict, List, Optional
from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, UniqueConstraint, func, text
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

    # ADR-010 (docs/architecture/ADR-010-synthetic-provenance-marker.md):
    # declares that every upsert of this model through bulk_upsert_rows
    # (src/db/upsert.py) automatically gets the "last real writer owns
    # provenance" rule (src/db/provenance.py). ClassVar excludes it from
    # SQLAlchemy's declarative mapping -- a plain class-level flag reads
    # better here than a mixin, since no other model in this file uses one
    # and the rule applies to exactly one model today.
    PROVENANCE_TRACKED: ClassVar[bool] = True

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
        doc="Detection/injection label: lowercase keys (e.g. co2_surge, "
            "synthetic_context) from the inject-test endpoint, or UPPER_CASE "
            "detector fault names once real detection starts persisting it. "
            "Does NOT mark synthetic provenance -- use source for that (ADR-010)."
    )
    source: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        doc="Provenance tag: 'live' for rows persisted by the live SCADA pipeline; "
            "'synthetic:<type>' / 'synthetic:context' for inject-test rows "
            "(ADR-010); None for CSV uploads and legacy un-backfilled rows."
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
    # Provenance marker, NOT a sensor-system identifier: "live" / "live:<...>"
    # (src/live/persistence.py), "synthetic:<...>" (src.config.synthetic_source,
    # ADR-010), or "csv" (src.config.CSV_UPLOAD_RAW_SOURCE) for a plain upload.
    # Until the source-column-collision fix, a CSV upload instead wrote the
    # CSV's own timestamp-column header string here (e.g. "Timestamp"), which
    # could collide with the "live"/"synthetic:" markers below -- see
    # docs/handoff/ and CSV_UPLOAD_RAW_SOURCE's docstring in src/config.py.
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


class AnomalyEpisodeRecord(Base):
    """Persisted, detection-time record of one fault window on one channel.

    This is the ORM model for the ``anomaly_episodes`` table. NOT the same
    concept as the query-time grouping GET /api/anomalies/episodes builds on
    the fly (``src/api/services/anomaly_episode_service.py``, which groups
    already-flagged ``sensor_readings`` rows by (channel, detector,
    direction) and persists nothing), and NOT the unrelated
    ``AnomalyEpisode`` dataclass used by the upload-grouping path
    (``src/models/episode_grouper.py``) or ``_AnomalyEpisode`` used by the
    live mock source (``src/live/sources/mock_episodes.py``) -- those are
    pre-existing, in-memory-only concepts with no table backing them. This
    model is the opposite of all three: one row is written *by a detector*
    when it declares a fault, and that row is the durable record -- nothing
    re-derives it from raw readings later. The class is named
    ``AnomalyEpisodeRecord`` (rather than ``AnomalyEpisode``) specifically to
    avoid colliding with those pre-existing names at any import site that
    touches both; the TABLE NAME collision with the query-time concept's
    output shape is separately deliberate (reviewed and accepted) and is not
    affected by this class-name choice.

    General by design: every column name is detector-agnostic. The first
    writer is the stuck-fault detector (``src/models/stuck_fault_detector.py``),
    but band-breach and calibration-drift detectors are expected to write the
    same columns — hence e.g. ``trigger_value`` rather than a stuck-specific
    name like ``frozen_value``.

    Timestamp contract (mirrors ``FaultWindow``,
    ``src/models/stuck_fault_detector.py:172`` — read that docstring before
    changing any of these four):
      - ``onset_ts``: first sample inside the fault.
      - ``end_ts``: the LAST FROZEN sample — ``FaultWindow.last_frozen_ts``.
        Known as soon as the fault is declared (updated as the fault
        continues) — not the release.
      - ``release_ts``: the first NON-frozen sample after the freeze ends.
        Two different samples, two different meanings; never collapse them.
      - ``detected_ts``: when the detector actually wrote this row, which
        lags ``onset_ts`` by however many consecutive samples the detector
        requires before calling it a fault. ``detected_ts - onset_ts`` is
        the detection latency; that value is computed at query time and
        deliberately has no column here.

    ``frozen_sample_count`` is counted over ``[onset_ts, end_ts]``: how many
    finite samples the detector actually saw stuck at the frozen value.

    ``raw_slot_count`` is NOT a tally of file rows that span covers. It is
    expected readings at the channel's pinned cadence across the freeze:

      - raw_slot_count = expected readings at the pinned cadence across the
        freeze: (end_ts - onset_ts) / cadence + 1.
      - Cadence is pinned once: branch 1, the pre-onset median when at
        least ``CADENCE_PIN_MIN_INTERVALS`` intervals exist; otherwise
        branch 2, the median of the freeze's own intervals at gate fire.
        Never recomputed.
      - The gap (``raw_slot_count - frozen_sample_count``) counts readings
        missing FOR THIS CHANNEL, whatever the cause. That includes whole
        rows the adapter dropped because a different required sensor was
        null. It is NOT proof this sensor went offline.
      - ``CSVAdapter`` drops rows with a null required sensor before
        detection, which is why the definition is expected slots, not rows
        counted.

    See ``src/models/stuck_fault_detector.py``'s ``ChannelFreezeState`` and
    ``expected_slot_count`` for the full derivation; this docstring mirrors
    that module's wording, not the other way around.

    ``duration_seconds`` is stored-and-derived, not authoritative: it is
    computed from ``end_ts - onset_ts`` (the frozen span observed so far) by
    whatever code writes/updates the row, and must be recomputed whenever
    either timestamp changes rather than written independently. It is not
    ``release_ts - onset_ts`` — that longer stuck-to-recovery span matters
    for alert suppression, not for describing how long the value was frozen.

    ``fault_type`` is deliberately NOT constrained by a DB CHECK here, unlike
    ``incidents.ck_incidents_fault_type``. Mirroring that constraint would
    mean a THIRD place (after migrations 011/016/019's literal tuples) that
    must be edited by hand every time the vocabulary grows, and a migration
    cannot import the app-side list without risking rewriting its own
    history if that list is later renamed (see migration 025's docstring for
    why). Validation is left to the application layer instead; a typo'd
    ``fault_type`` is accepted silently today. See the migration docstring
    for the full trade-off.

    ``source`` mirrors ``SensorReading.source`` (ADR-010): ``None`` for a
    real detection, ``'synthetic:<type>'`` for one produced by the
    inject-test endpoint. Same string, same convention, same column name —
    deliberately not reinvented here.

    Upsert key: ``uq_anomaly_episodes_tenant_gh_channel_detector_onset``
    -- UNIQUE (tenant_id, greenhouse_id, channel, detector, onset_ts) -- is
    the conflict target the Part B writer upserts against: it updates
    ``end_ts`` on an already-open fault instead of inserting a duplicate
    row, and it is what makes a re-upload of the same file idempotent. See
    the migration's "Constraints" section for the full rationale.

    ``release_ts`` / ``is_ongoing`` invariant: enforced by
    ``ck_anomaly_episodes_release_ts_null_while_ongoing`` -- CHECK (NOT
    is_ongoing OR release_ts IS NULL) -- in ONE direction only. An ongoing
    fault cannot have a release timestamp, but a closed (is_ongoing=False)
    fault is NOT required to have one: drift and band-breach episodes can
    close without ever observing a first normal sample to release against,
    unlike a stuck-fault freeze, which always has one.
    Do not tighten this to a two-way constraint.
    """

    __tablename__ = "anomaly_episodes"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_generate_uuid,
        doc="Unique episode UUID (string for cross-backend portability, "
            "matching Incident's convention)")
    tenant_id: Mapped[str] = mapped_column(
        String(255), nullable=False,
        doc="Identifier for the multi-tenant customer entity")
    greenhouse_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default",
        doc="Identifier for the specific greenhouse or sensor zone")
    channel: Mapped[str] = mapped_column(
        String(255), nullable=False,
        doc="Raw column name the fault was detected on, e.g. 'temperature'")
    detector: Mapped[str] = mapped_column(
        String(64), nullable=False,
        doc="Detector name that wrote this episode, e.g. 'stuck_fault', "
            "'band_breach', 'calibration_drift'")
    fault_type: Mapped[str] = mapped_column(
        String(64), nullable=False,
        doc="Fault label, e.g. stuck_fault, calibration_drift, "
            "temp_band_high. Not DB-constrained -- see class docstring")

    onset_ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        doc="First sample inside the fault window. Always known once the "
            "detector declares a fault")
    end_ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        doc="Last frozen sample (FaultWindow.last_frozen_ts). Updated as "
            "an ongoing fault continues; see class docstring")
    detected_ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        doc="When the detector actually wrote this row, lagging onset_ts "
            "by its required consecutive-sample threshold")
    release_ts: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
        doc="First non-frozen sample after the freeze ends. NULL while "
            "is_ongoing is True -- the release has not happened yet. "
            "Enforced one-directionally by "
            "ck_anomaly_episodes_release_ts_null_while_ongoing -- see "
            "class docstring")

    duration_seconds: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        doc="Stored-and-derived: end_ts - onset_ts in seconds. Recomputed "
            "by the writer on every update, not independently authoritative")
    frozen_sample_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        doc="Finite samples the detector counted as frozen over "
            "[onset_ts, end_ts]. Computed by WallclockStuckDetector and "
            "persisted by anomaly_episode_writer.py, which rejects a stuck "
            "row whose counts are null -- see class docstring")
    raw_slot_count: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        doc="Expected readings at the pinned cadence across "
            "[onset_ts, end_ts] -- NOT a row tally. Gap vs "
            "frozen_sample_count counts readings missing for this "
            "channel for any cause, including rows the upload adapter "
            "dropped over a different sensor's null; see class docstring")

    severity: Mapped[str] = mapped_column(
        String(16), nullable=False,
        doc="Severity label (e.g. critical, warning) assigned at detection")
    trigger_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
        doc="Representative raw value for the fault, e.g. the frozen "
            "reading. NULL for detectors with no single representative "
            "value (e.g. reconstruction-error-based)")
    is_ongoing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        doc="True while the fault has not yet released. release_ts and "
            "duration_seconds are not meaningful while this is True")
    source: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True,
        doc="Provenance tag mirroring SensorReading.source (ADR-010): "
            "None for a real detection, 'synthetic:<type>' for inject-test")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "greenhouse_id", "channel", "detector", "onset_ts",
            name="uq_anomaly_episodes_tenant_gh_channel_detector_onset",
        ),
        CheckConstraint(
            "NOT is_ongoing OR release_ts IS NULL",
            name="ck_anomaly_episodes_release_ts_null_while_ongoing",
        ),
        Index(
            "ix_anomaly_episodes_tenant_gh_channel_onset",
            "tenant_id", "greenhouse_id", "channel", "onset_ts",
        ),
        Index(
            "ix_anomaly_episodes_ongoing_tenant_gh",
            "tenant_id", "greenhouse_id",
            postgresql_where=text("is_ongoing"),
            sqlite_where=text("is_ongoing"),
        ),
        Index(
            "ix_anomaly_episodes_fault_type_detector",
            "fault_type", "detector",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AnomalyEpisodeRecord(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"channel={self.channel!r}, fault_type={self.fault_type!r}, "
            f"is_ongoing={self.is_ongoing})>"
        )


class LiveScopeState(Base):
    """Operator-chosen desired state for one live (tenant, greenhouse) scope.

    Why this table exists
    ---------------------
    Before it, the only record that an operator had pressed "Go Live" was
    :class:`~src.live.runtime.LiveRuntime`'s in-process task map plus the
    ``_suppressed_scopes`` set in :mod:`src.live.scope_control`.  Both die with
    the uvicorn process, so every redeploy silently returned every scope to
    idle with no resume and no signal.  Persisting the operator's intent lets
    the API restore it on boot.

    This is **desired** state, not observed state.  ``live`` means "the
    operator wants connectors running for this scope"; it does not mean they
    are running right now, and it says nothing about whether data is actually
    arriving (see the freshness fields on ``GET /api/live/status`` for that).

    ``LIVE_AUTOSTART_ENABLED=false`` semantics are preserved: a scope with no
    row here, or a row saying ``paused``, never starts on its own.  Only an
    explicit ``POST /api/live/resume`` writes ``live``.
    """

    __tablename__ = "live_scope_state"

    # Column values for desired_state. Mirrored by the CheckConstraint below
    # and by src.api.services.live_scope_state_service.
    DESIRED_LIVE: ClassVar[str] = "live"
    DESIRED_PAUSED: ClassVar[str] = "paused"

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
    desired_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        doc="Operator intent: 'live' (resume on boot) or 'paused' (stay idle)",
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        doc="When the operator last changed this scope's desired state",
    )

    __table_args__ = (
        CheckConstraint(
            "desired_state IN ('live', 'paused')",
            name="ck_live_scope_state_desired_state",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<LiveScopeState(tenant_id={self.tenant_id!r},"
            f" greenhouse_id={self.greenhouse_id!r},"
            f" desired_state={self.desired_state!r})>"
        )

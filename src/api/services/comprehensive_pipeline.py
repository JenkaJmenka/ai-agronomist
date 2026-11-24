"""
Comprehensive anomaly inference pipeline orchestration.

Wires Detection -> Attribution -> Pattern -> Confidence -> Root Cause ->
Recommendations -> Incident Store -> Learning progress -> Metrics.
"""

import copy
import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, TypedDict

import numpy as np
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import (
    ACTIVE_LEARNING_MIN_QUALITY_LABELS,
    BIAS_EMA_SPAN,
    BIAS_EMA_THRESHOLD,
    BIAS_EMA_THRESHOLD_BY_SENSOR,
    CUSUM_H_SIGMA,
    CUSUM_K_SIGMA,
    DIURNAL_PROFILE_HISTORY_DAYS,
    DROPOUT_CONSECUTIVE_P,
    EWMA_ALPHA,
    EWMA_K,
    INPUT_DIM,
    NUMERICAL_EPSILON,
    POT_BUFFER_SIZE,
    POT_FPR,
    POT_Q,
    PSYCHROMETRIC_HD_TOLERANCE,
    SEQ_LEN,
    SENSOR_NAMES,
    TEMP_RANGE,
    HUMIDITY_RANGE,
    CO2_RANGE,
    LIGHT_RANGE,
)
from src.db.incident_memory import IncidentMemory
from src.db.models import LearningState, SensorReading
from src.models.anomaly_attributor import AnomalyAttributor
from src.models.bias_detector import BiasOffsetDetector as BiasEMADetector
from src.models.multivariate_mahalanobis_detector import (
    MultivariateMahalanobisDetector,
)
from src.models.band_breach_detector import BAND_TYPE_TO_VOTE, BandBreachDetector  # noqa: F401
from src.models.confidence_scorer import ConfidenceScorer
from src.models.data_utils import SENSOR_RANGES
from src.models.cusum_drift_detector import CUSUMDriftDetector, EMABaselineEstimator
from src.models.dropout_detector import DropoutDetector
from src.models.ensemble_detector import EnsembleAnomalyDetector, LstmVaeDetector
# Kept although unused here: tests patch ``comprehensive_pipeline.NoiseDetector.detect``
# and ``...BandBreachDetector.detect`` by this module path.
from src.models.noise_detector import NoiseDetector  # noqa: F401
from src.models.ewma_tracker import EWMATracker
from src.models.fault_classifier import FaultClassifier
from src.models.inference import AnomalyDetector, ModelScores
from src.models.scoring import (
    API_PROFILE,
    LstmDecisionRule,
    NoiseBaseline,
    OobPolicy,
    ScoringContext,
    ScoringProfile,
    SpikeConfirmMode,
)
from src.models.scoring.precedence import (
    RULE_PRECEDENCE,
    RULE_VOTE_PROFILES,
    apply_rule_votes,
    band_promotion,
    order_rule_votes,
    primary_rule_is_setpoint,
    rule_reasoning,
)
from src.models.scoring.lstm import lstm_flag, lstm_threshold, scaled_scores
from src.models.scoring.votes import (
    change_point_vote,
    diurnal_vote,
    noise_vote,
    oob_vote,
    spike_mad_vote,
    window_state,
)
from src.models.scoring.window_rules import (
    SETPOINT_TYPE_TO_VOTE,
    band_findings as band_findings_vote,
    dual_sensor_votes,
    setpoint_votes,
)
from src.api.services.scoring_history import (
    DbHistoryProvider,
    load_diurnal_profiles,
    load_seasonal_scaler,
)
from src.models.operational_config_store import get_operational_config_store
from src.models.pattern_detector import PatternDetector
from src.models.physics_validator import PhysicsValidator
from src.models.psychrometric_validator import (
    calculate_humidity_deficit,
    validate_row as psychrometric_validate_row,
)
from src.models.pot_threshold import POTThresholdFitter
from src.models.recommendation_engine import RecommendationEngine
from src.models.retraining_orchestrator import RetrainingOrchestrator
from src.models.root_cause_analyzer import RootCauseAnalyzer
from src.models.seasonal_scaler import (
    SeasonalScaler,
)
from src.monitoring.anomaly_metrics import AnomalyMetrics
from src.utils.lru import LRUDict

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "2.0"

# The pipeline's default scoring profile (ADR-014 step A4). The live
# enrichment (src/live/enrichment.py, which builds a pipeline per episode and
# passes no profile) and the inference routes run this class identically
# today: the same votes, and the adaptive max(EWMA, POT) LSTM decision. They
# differ only in call arguments (live passes noise_clean_windows and no
# climate or sensor pairs). So the default is API_PROFILE with the noise
# baseline calibrated from clean windows when the caller passes them, which
# is what both callers get today. A6 moves live to its own profile.
PIPELINE_PROFILE: ScoringProfile = replace(
    API_PROFILE, noise_baseline=NoiseBaseline.RING_CALIBRATED
)


def _check_pipeline_profile(profile: ScoringProfile) -> ScoringProfile:
    """Reject a profile whose behaviour the pipeline does not implement.

    The pipeline gates a window on an out-of-bounds hit, votes the spike by
    lag, and takes the LSTM cutoff from trackers or a fixed value. The batch
    profile's clamp-and-continue, look-ahead spike and per-file percentile
    are upload stages (A7).

    Raises:
        ValueError: For such a profile.
    """
    if profile.oob_policy is not OobPolicy.GATE:
        raise ValueError(f"pipeline needs oob_policy=gate, got {profile.oob_policy.value}")
    if profile.spike_confirm.mode is not SpikeConfirmMode.LAG:
        raise ValueError("pipeline needs lag spike confirmation, not look-ahead")
    if profile.lstm_decision.rule is LstmDecisionRule.FILE_PERCENTILE_ROBUST:
        raise ValueError("pipeline cannot use a per-file LSTM threshold")
    return profile


def _json_safe(value: Any) -> Any:
    """Recursively replace non-finite floats with None for a JSON column.

    SQLite accepts the non-standard ``Infinity``/``NaN`` tokens, PostgreSQL
    rejects them with ``invalid input syntax for type json``.  A single
    non-finite leaf anywhere in the payload therefore fails the whole write in
    production while passing every local test.

    This is a net at the write boundary — values should be finite before they
    get here (see ``POTThresholdFitter.to_dict``).  It exists because the
    failure mode is silent: the save is caught and logged as non-critical, so
    the tenant's learning state simply stops persisting with nothing surfacing
    to the operator.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# Map PhysicsValidator reason strings to fault type values.
_PHYSICS_REASON_TO_FAULT: Dict[str, str] = {
    "psychrometric_impossibility": "spike_fault",
    # humidity_fault is the documented psychrometric label (BACKLOG-20).
    # It is allowed by ck_incidents_fault_type as of migration 029.
    # FaultType in src/models/fault_classifier.py now lists HUMIDITY_FAULT;
    # the incident API also includes it in its union for compatibility.
    "psychrometric_inconsistency": "humidity_fault",
    "nocturnal_light": "spike_fault",
    "co2_out_of_range": "calibration_drift",
    "temperature_out_of_range": "spike_fault",
}

# Rule-vote precedence, vote profiles and band promotion live in the scoring
# core (src/models/scoring/precedence.py, ADR-014 A4b). These aliases keep the
# old private names importable from here.
_RULE_PRECEDENCE = RULE_PRECEDENCE
_RULE_VOTE_PROFILES = RULE_VOTE_PROFILES
build_rule_reasoning = rule_reasoning

# Maps SetpointAwareScadaDetector anomaly_type constants to rule-vote keys.
# The table is the scoring core's (src/models/scoring/window_rules.py); this
# plain dict stays for callers that import it from here.
_SCADA_TYPE_TO_VOTE: Dict[Any, str] = dict(SETPOINT_TYPE_TO_VOTE)

# Sensor ranges in physical units for denormalization.
_DENORM_RANGES = np.array(
    [TEMP_RANGE, HUMIDITY_RANGE, CO2_RANGE, LIGHT_RANGE],
    dtype=np.float32,
)

# When True, the history-based Mahalanobis path queries the DB and may vote.
# False until the reference distribution is conditioned on time-of-day and
# the cutoff derives from the history's own distance distribution (not a fixed
# chi-squared table).  Re-arming is a one-line change here.
_MAHALANOBIS_HISTORY_VOTE_ENABLED: bool = False

# Minimum number of stored incidents before enabling dynamic thresholds.
_MIN_INCIDENTS_FOR_DYNAMIC_THRESHOLD: int = 50


async def _load_operational_overrides_if_uncached(
    customer_id: str,
    greenhouse_id: str,
    session: AsyncSession,
) -> None:
    """Load saved band overrides when this scope is not already cached.

    Tests set overrides in memory. A cached key is left alone, including
    an empty dict. ``get_raw_overrides`` cannot tell those apart from a
    scope that was never loaded. A missing table or row is logged and
    the run continues on code defaults.
    """
    store = get_operational_config_store()
    key = (customer_id, greenhouse_id)
    if key in store._overrides:
        return
    try:
        await store.load_from_db(customer_id, greenhouse_id, session)
    except Exception:
        logger.warning(
            "Operational config table unavailable for tenant %s greenhouse %s; "
            "continuing without saved band overrides",
            customer_id,
            greenhouse_id,
            exc_info=True,
        )
        rollback = getattr(session, "rollback", None)
        if rollback is not None:
            try:
                await rollback()
            except Exception:
                logger.warning(
                    "Operational config session rollback failed for tenant %s "
                    "greenhouse %s",
                    customer_id,
                    greenhouse_id,
                    exc_info=True,
                )
        return
    if key not in store._overrides:
        logger.info(
            "No operational config row for tenant %s greenhouse %s; "
            "continuing with code defaults",
            customer_id,
            greenhouse_id,
        )


def _denormalize_sequence(normalized: np.ndarray) -> np.ndarray:
    """Convert normalized [0,1] sensor data back to physical units."""
    mins = _DENORM_RANGES[:, 0]
    spans = _DENORM_RANGES[:, 1] - _DENORM_RANGES[:, 0]
    return normalized * spans + mins


def _as_utc(ts: datetime) -> datetime:
    """Attach UTC to a naive timestamp; convert an aware one to UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _finite_sensor_row(row: object) -> Optional[list[float]]:
    """One physical row in ``SENSOR_NAMES`` order, or None if any value is unusable."""
    values: list[float] = []
    for name in SENSOR_NAMES:
        raw = getattr(row, name, None)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        value = float(raw)
        if not math.isfinite(value):
            return None
        values.append(value)
    return values


def _finite_sensor_matrix(rows: list) -> np.ndarray:
    """Physical history. A row is kept only when every sensor value is finite."""
    kept: list[list[float]] = []
    for row in rows:
        values = _finite_sensor_row(row)
        if values is not None:
            kept.append(values)
    width = len(SENSOR_NAMES)
    if not kept:
        return np.empty((0, width), dtype=float)
    return np.asarray(kept, dtype=float)


async def _query_physical_history(
    db: AsyncSession,
    *,
    customer_id: str,
    greenhouse_id: str,
    before: Optional[datetime],
) -> np.ndarray:
    """Finite ``SensorReading`` rows over the diurnal trailing window.

    ``before`` anchors the window end (and drops the reading under test
    when the sequence carries a timestamp, so that point is not inside its
    own covariance fit). When ``before`` is None, wall-clock now is used.

    Synthetic rows (``source`` starting with ``"synthetic:"``) are excluded
    so that injected test episodes do not distort the covariance estimate.
    NULL source rows (CSV uploads, legacy rows) are kept.

    The query selects only the four sensor columns (no full ORM rows) and
    is capped at ``DIURNAL_PROFILE_HISTORY_DAYS * 288`` rows (5-minute
    cadence, newest first) to bound memory and query time.
    """
    anchor = before if before is not None else datetime.now(timezone.utc)
    since = anchor - timedelta(days=DIURNAL_PROFILE_HISTORY_DAYS)
    row_limit = DIURNAL_PROFILE_HISTORY_DAYS * 288
    stmt = (
        select(
            SensorReading.temperature,
            SensorReading.humidity,
            SensorReading.co2,
            SensorReading.light,
        )
        .where(
            SensorReading.tenant_id == customer_id,
            SensorReading.greenhouse_id == greenhouse_id,
            SensorReading.timestamp >= since,
            SensorReading.timestamp < anchor,
        )
        .where(
            or_(
                SensorReading.source.is_(None),
                ~SensorReading.source.startswith("synthetic:"),
            )
        )
        .order_by(SensorReading.timestamp.desc())
        .limit(row_limit)
    )
    result = await db.execute(stmt)
    rows = result.all()
    return _finite_sensor_matrix(rows)


async def _rollback_quietly(
    db: AsyncSession,
    customer_id: str,
    greenhouse_id: str,
) -> None:
    """Clear an aborted transaction so the rest of the run can continue."""
    rollback = getattr(db, "rollback", None)
    if rollback is None:
        return
    try:
        await rollback()
    except Exception:
        logger.warning(
            "Mahalanobis history session rollback failed for tenant %s greenhouse %s",
            customer_id,
            greenhouse_id,
            exc_info=True,
        )


def build_default_baseline(num_sequences: int = 10) -> np.ndarray:
    """Synthetic normal baseline from sensor range midpoints."""
    mids = np.array(
        [(lo + hi) / 2.0 for lo, hi in SENSOR_RANGES],
        dtype=np.float32,
    )
    template = np.tile(mids, (SEQ_LEN, 1))
    return np.stack([template.copy() for _ in range(num_sequences)])


def _observe_online_learner(
    customer_id: str,
    sequence: np.ndarray,
    anomaly_score: float,
) -> None:
    """Feed a detected anomaly sequence to the per-tenant online learner.

    Uses the same singleton manager as src.api.routes.learning so all callers
    share one set of per-tenant model weights.  The anomaly_score is retained
    as a parameter for observability (logged below) and future extension.
    The current OnlineLearner.observe() API accepts only the raw sequence.
    """
    # Import here to avoid a circular import at module load time.
    from src.api.routes.learning import _get_manager  # noqa: PLC0415

    manager = _get_manager()
    manager.observe(customer_id, sequence)
    logger.debug(
        "Online learner observed anomaly sequence for tenant %s (score=%.4f)",
        customer_id,
        anomaly_score,
    )


class PipelineResult(TypedDict):
    """All keys returned by _run_with_components and _suppressed_result."""

    is_anomaly: bool
    anomaly_score: float
    confidence: float
    confidence_level: str
    pattern_type: str
    fault_type: Optional[str]
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
    incident_id: Optional[str]
    detector_scores: Dict[str, float]
    ensemble_score: float
    ensemble_agreement: int
    physics_validation: Optional[str]
    learning_progress: Optional[Dict[str, int]]
    timestamp: str
    pipeline_version: str
    scada_anomalies: List[Dict[str, Any]]


class ComprehensiveInferencePipeline:
    """Runs the full Phase 1.9.1 anomaly detection pipeline."""

    def __init__(
        self,
        detector: AnomalyDetector,
        confidence_scorer: Optional[ConfidenceScorer] = None,
        root_cause_analyzer: Optional[RootCauseAnalyzer] = None,
        recommendation_engine: Optional[RecommendationEngine] = None,
        metrics: Optional[AnomalyMetrics] = None,
        orchestrators: Optional[Dict[str, RetrainingOrchestrator]] = None,
        profile: Optional[ScoringProfile] = None,
    ) -> None:
        """Build a pipeline.

        Args:
            profile: The scoring profile whose votes and LSTM decision the
                pipeline runs. Defaults to ``PIPELINE_PROFILE``: the live
                enrichment and the inference routes share this class today
                and score identically, so one default keeps both as they
                were. A profile the pipeline cannot honour raises
                ``ValueError``.
        """
        self._profile = _check_pipeline_profile(profile or PIPELINE_PROFILE)
        self.detector = detector
        self.confidence_scorer = confidence_scorer or ConfidenceScorer()
        self.root_cause_analyzer = root_cause_analyzer or RootCauseAnalyzer()
        self.recommendation_engine = (
            recommendation_engine or RecommendationEngine()
        )
        self.metrics = metrics
        self._orchestrators = orchestrators if orchestrators is not None else {}
        self._ensemble: Optional[EnsembleAnomalyDetector] = None
        self._tracker_cache = LRUDict(
            max_size=100,
            on_evict=lambda customer_id, _: (
                self._evict_cusum_ema_caches(customer_id),
                self._evict_diurnal_cache(customer_id),
            ),
        )
        # Polling dropout detector (shared across tenants — stateless).
        self._dropout_detector = DropoutDetector()
        # Per-(tenant, greenhouse) CUSUM drift detectors (stateful, persisted in LearningState).
        # Keyed by (customer_id, greenhouse_id) so each greenhouse has an independent
        # drift accumulator; LearningState persistence remains at tenant level.
        self._cusum_cache: dict[tuple[str, str], CUSUMDriftDetector] = {}
        # Per-(tenant, greenhouse) EMA baseline estimators for CUSUM standardized residuals.
        self._ema_cache: dict[tuple[str, str], EMABaselineEstimator] = {}
        # Phase B SCADA detectors (stateless, shared).
        # Sensitivity layer (plan P3): joint-implausibility + change-point,
        # both stateless and shared. They run on the physical-unit window.
        self._mahalanobis_detector = MultivariateMahalanobisDetector()
        # Per-(tenant, greenhouse, anchor_date) cached diurnal profiles:
        # {(customer_id, greenhouse_id, date): {sensor: {hour: (mean, std)}}}
        self._diurnal_profiles_cache: dict[tuple, Dict[str, Dict[int, tuple]]] = {}
        # Physical history for the Mahalanobis fit, keyed by tenant and greenhouse.
        # A live reading builds a new pipeline, so a hit only repeats inside one run.
        self._mahalanobis_history_cache: dict[tuple[str, str], np.ndarray] = {}
        # Component caching
        self._default_baseline: Optional[np.ndarray] = None
        self._cached_baseline: Optional[np.ndarray] = None
        self._cached_baseline_hash: Optional[str] = None
        self._cached_attributor: Optional[AnomalyAttributor] = None
        self._cached_pattern_detector: Optional[PatternDetector] = None

    def _get_ensemble(self) -> EnsembleAnomalyDetector:
        if self._ensemble is None:
            lstm = LstmVaeDetector(self.detector)
            self._ensemble = EnsembleAnomalyDetector(lstm_detector=lstm)
        return self._ensemble

    @staticmethod
    def _peak_sensor_values(sequence: np.ndarray) -> Dict[str, float]:
        """Sensor readings at the last timestep (peak window)."""
        last = sequence[-1]
        return {
            name: float(last[idx])
            for idx, name in enumerate(SENSOR_NAMES[: sequence.shape[1]])
        }

    def _prepare_baseline(
        self, baseline_sequences: Optional[List[List[List[float]]]]
    ) -> np.ndarray:
        if baseline_sequences:
            return np.asarray(baseline_sequences, dtype=np.float32)
        if self._default_baseline is None:
            self._default_baseline = build_default_baseline()
        return self._default_baseline

    def _get_cached_components(
        self, baseline: np.ndarray
    ) -> tuple[np.ndarray, AnomalyAttributor, PatternDetector]:
        import hashlib
        baseline_hash = hashlib.sha256(np.ascontiguousarray(baseline)).hexdigest()

        if (
            self._cached_baseline is not None
            and (baseline is self._cached_baseline or self._cached_baseline_hash == baseline_hash)
            and self._cached_attributor is not None
            and self._cached_pattern_detector is not None
        ):
            return self._cached_baseline, self._cached_attributor, self._cached_pattern_detector

        # Deep-copy the model: AnomalyAttributor.__init__ mutates it in place
        # (.to(device), .eval()), and self.detector.model is shared with the
        # live scoring path (DetectorResolver's cached instance). Mutating it
        # here races torch.nn.RNNBase._apply against a concurrent forward
        # pass on that same instance (BACKLOG-19).
        attributor = AnomalyAttributor(copy.deepcopy(self.detector.model))
        attributor.set_baseline(baseline)

        pattern_detector = PatternDetector()
        pattern_detector.set_baseline(baseline)

        self._cached_baseline = baseline
        self._cached_baseline_hash = baseline_hash
        self._cached_attributor = attributor
        self._cached_pattern_detector = pattern_detector

        return baseline, attributor, pattern_detector

    async def run(
        self,
        customer_id: str,
        sequence: List[List[float]],
        db: AsyncSession,
        greenhouse_id: str = "default",
        baseline_sequences: Optional[List[List[List[float]]]] = None,
        sensor_pairs: Optional[List[Dict[str, Any]]] = None,
        sequence_timestamp: Optional[str] = None,
        climate_context: Optional[Dict[str, Any]] = None,
        noise_clean_windows: Any = None,
    ) -> Dict[str, Any]:
        """Execute the full pipeline for one sequence.

        ``climate_context`` may carry climate-computer extras
        (``t_setpoint``, ``t_plant``, ``t_outdoor``, ``i_solar``, …).

        ``noise_clean_windows`` are physical-unit windows for
        ``calibrate_noise_baseline``. Omit them to keep the fixed noise
        map. Do not pass the normalized LSTM baseline.
        """
        await _load_operational_overrides_if_uncached(
            customer_id, greenhouse_id, db
        )
        seq_array = np.asarray(sequence, dtype=np.float32)
        if seq_array.shape != (SEQ_LEN, INPUT_DIM):
            raise ValueError(
                f"sequence shape must be ({SEQ_LEN}, {INPUT_DIM}), "
                f"got {seq_array.shape}"
            )

        baseline = self._prepare_baseline(baseline_sequences)
        baseline, attributor, pattern_detector = self._get_cached_components(baseline)

        return await self._run_with_components(
            customer_id=customer_id,
            seq_array=seq_array,
            sequence_raw=sequence,
            db=db,
            greenhouse_id=greenhouse_id,
            baseline=baseline,
            attributor=attributor,
            pattern_detector=pattern_detector,
            sensor_pairs=sensor_pairs,
            sequence_timestamp=sequence_timestamp,
            climate_context=climate_context,
            noise_clean_windows=noise_clean_windows,
        )

    async def run_batch(
        self,
        customer_id: str,
        sequences: List[np.ndarray],
        db: AsyncSession,
        greenhouse_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Execute the full pipeline for multiple sequences.

        Creates or retrieves cached ``AnomalyAttributor`` and ``PatternDetector``,
        calls ``set_baseline`` once, then reuses them across all sequences
        in the batch.
        """
        baseline = self._prepare_baseline(None)
        baseline, attributor, pattern_detector = self._get_cached_components(baseline)

        results: List[Dict[str, Any]] = []
        for seq_array in sequences:
            if seq_array.shape != (SEQ_LEN, INPUT_DIM):
                raise ValueError(
                    f"sequence shape must be ({SEQ_LEN}, {INPUT_DIM}), "
                    f"got {seq_array.shape}"
                )
            result = await self._run_with_components(
                customer_id=customer_id,
                seq_array=seq_array,
                sequence_raw=seq_array.tolist(),
                db=db,
                greenhouse_id=greenhouse_id or "default",
                baseline=baseline,
                attributor=attributor,
                pattern_detector=pattern_detector,
            )
            results.append(result)
        return results

    async def _run_with_components(
        self,
        customer_id: str,
        seq_array: np.ndarray,
        sequence_raw: List[List[float]],
        db: AsyncSession,
        greenhouse_id: str,
        baseline: np.ndarray,
        attributor: AnomalyAttributor,
        pattern_detector: PatternDetector,
        sensor_pairs: Optional[List[Dict[str, Any]]] = None,
        sequence_timestamp: Optional[str] = None,
        climate_context: Optional[Dict[str, Any]] = None,
        noise_clean_windows: Any = None,
    ) -> "PipelineResult":
        """Core pipeline logic shared by ``run()`` and ``run_batch()``."""
        now = datetime.now(timezone.utc)
        seq_ts = now
        history_before: Optional[datetime] = None
        if sequence_timestamp:
            try:
                seq_ts = datetime.fromisoformat(sequence_timestamp)
                history_before = _as_utc(seq_ts)
            except (ValueError, TypeError):
                pass

        # 0. Polling dropout check — short-circuit before expensive forward pass.
        # Convert List[List[float]] to List[Dict[str, Any]] for DropoutDetector.
        # Values are already parsed floats, so None/all-None detection applies
        # to raw API payloads that allow None (future extension point).
        _seq_as_dicts = [
            {name: val for name, val in zip(SENSOR_NAMES[: len(row)], row)}
            for row in sequence_raw
        ]
        if self._dropout_detector.detect(_seq_as_dicts):
            _dropout_reason = (
                "Polling dropout: {:d} consecutive sequence steps each have at least ".format(
                    DROPOUT_CONSECUTIVE_P
                )
                + "one missing sensor value"
            )
            logger.warning(
                "Polling dropout detected for tenant %s: %s",
                customer_id,
                _dropout_reason,
            )
            return await self._persist_gate_result(
                self._dropout_result(reason=_dropout_reason),
                customer_id=customer_id,
                greenhouse_id=greenhouse_id,
                seq_array=seq_array,
                sequence_raw=sequence_raw,
                db=db,
            )

        # Rule-detector votes (fusion, plan P2). Fired rules no longer
        # short-circuit: they confirm the anomaly and steer fault typing
        # while the LSTM/ensemble path still runs, so multi-fault sequences
        # keep full attribution, pattern scores, and recommendations.
        # Keyed by _RULE_VOTE_PROFILES name → affected sensors/variables.
        #
        # The detector votes themselves are the ADR-014 scoring core
        # (src/models/scoring, step A4). This method hands the core the
        # window, gathers each vote in the order the pipeline always had
        # (setpoint, noise, OOB gate, spike, bias-dual, diurnal, BOCPD) and
        # applies precedence (order_rule_votes) afterwards.
        rule_votes: dict[str, list[str]] = {}

        # The core reads physical units (°C, %, ppm, lux), as
        # SENSOR_NOISE_TARGETS in src/config.py does.
        _denormed_for_noise = _denormalize_sequence(seq_array)
        _profile = self._profile
        _vote_context = ScoringContext(
            tenant_id=customer_id,
            greenhouse_id=greenhouse_id,
            sequence_timestamp=seq_ts,
            profile=_profile,
            climate=climate_context,
            sensor_pairs=tuple(sensor_pairs or ()),
        )

        # 0a+. Setpoint-aware climate / RH / microclimate / flatline rules.
        # Night light sits at zero for hours: a light-only flatline neither
        # votes nor lands in scada_anomalies (handled inside the core).
        _setpoint = setpoint_votes(_denormed_for_noise, _vote_context, _profile)
        scada_anomalies: list[dict[str, Any]] = [dict(f) for f in _setpoint.findings]
        for _setpoint_key, _setpoint_sensors in _setpoint.votes.items():
            rule_votes[_setpoint_key] = list(_setpoint_sensors)
        for _setpoint_finding in scada_anomalies:
            if SETPOINT_TYPE_TO_VOTE.get(_setpoint_finding["anomaly_type"]):
                logger.warning(
                    "Setpoint SCADA %s for tenant %s: %s",
                    _setpoint_finding["anomaly_type"],
                    customer_id,
                    _setpoint_finding["reasoning"],
                )

        # 0a++. Operator crop band breaches (day/night min–max from OperationalConfig).
        # Band breaches are process/climate deviations, NOT sensor-fault evidence.
        # Collected in band_findings (separate from rule_votes) to preserve
        # fault-type precedence:
        #   (1) sensor-fault rule votes → (2) physics → (3) LSTM → (4) band LAST
        # Ensemble is non-voting and is not in this chain.
        # Sensor-trust gate and last-resort fusion happen after all sensor-fault
        # detectors have voted (step 5c-gate / 5c-last below).
        # An unconfigured tenant is on the default tomato profile. Those
        # breaches stay on scada_anomalies and do not set is_anomaly.
        # Promotion requires a non-empty raw override for this greenhouse.
        _op_store = get_operational_config_store()
        _op_config = _op_store.get(customer_id, greenhouse_id)
        _band_overrides_configured = bool(
            _op_store.get_raw_overrides(customer_id, greenhouse_id)
        )
        # What every vote of this window reads. The seasonal scaler and the
        # diurnal profiles are DB lookups: they are awaited at the points
        # the pipeline always awaited them (the OOB gate returns before the
        # diurnal query) and swapped in with ``replace``.
        # ``window_state(context)`` is re-derived after each lookup lands:
        # the provider returns the identity scaler / no profiles until then.
        _history = DbHistoryProvider(
            customer_id,
            greenhouse_id,
            operational_config=_op_config,
            noise_clean_windows=noise_clean_windows,
            diurnal_cache=self._diurnal_profiles_cache,
        )
        _vote_context = replace(_vote_context, history=_history)
        _window_state = window_state(_vote_context)
        _season_key = _window_state.season
        band_findings: list[dict[str, Any]] = []
        for _band_finding in band_findings_vote(
            _denormed_for_noise, _vote_context, _profile, _window_state
        ).findings:
            finding_dict = dict(_band_finding)
            scada_anomalies.append(finding_dict)
            band_findings.append(finding_dict)
            logger.warning(
                "Band breach %s for tenant %s: %s",
                finding_dict["anomaly_type"],
                customer_id,
                finding_dict["reasoning"],
            )

        # Seasonal σ scaler — factor 1.0 when baselines are sparse / absent.
        await _history.load_seasonal_scaler(db, _season_key)
        _window_state = window_state(_vote_context)

        # 0a. Noise / precision degradation (SCADA spec 1.4): window-level
        # variance ratio, light excluded. Calibrated baselines apply only
        # when the caller passed physical clean windows and the profile
        # calibrates (NoiseBaseline.RING_CALIBRATED).
        _noise = noise_vote(_denormed_for_noise, _vote_context, _profile, _window_state)
        if _noise.fired:
            logger.warning(
                "Noise/precision degradation detected for tenant %s — sensors: %s",
                customer_id,
                list(_noise.votes["noise"]),
            )
            rule_votes["noise"] = list(_noise.votes["noise"])

        # 0b. Out-of-bounds check — short-circuit on hard physical limits
        # (OobPolicy.GATE; the pipeline accepts no other policy).
        _oob = oob_vote(_denormed_for_noise, _vote_context, _profile, _window_state)
        if _oob.fired:
            oob_alarmed = list(_oob.votes["out_of_bounds"])
            logger.warning(
                "Out-of-bounds readings for tenant %s — sensors: %s",
                customer_id,
                oob_alarmed,
            )
            return await self._persist_gate_result(
                self._build_oob_result(oob_alarmed),
                customer_id=customer_id,
                greenhouse_id=greenhouse_id,
                seq_array=seq_array,
                sequence_raw=sequence_raw,
                db=db,
            )

        # 0c. Spike (MAD): exactly one timestep votes (t - lag, confirmed by
        # a revert within the lag; lag 0 votes the last step unconfirmed).
        # Pairs, floors and the seasonal scaling come from the profile.
        _spike = spike_mad_vote(_denormed_for_noise, _vote_context, _profile, _window_state)
        if _spike.fired:
            logger.warning(
                "MAD spike detected for tenant %s — sensors: %s",
                customer_id,
                list(_spike.votes["spike_mad"]),
            )
            rule_votes["spike_mad"] = list(_spike.votes["spike_mad"])

        # 0d. Bias (EMA) check — detect sudden persistent offsets per sensor.
        # Thresholds are per sensor: the physical scales differ by orders of
        # magnitude, so a shared absolute limit over-flags lux/ppm channels.
        # BIAS_EMA_SPAN is 288 samples (24 h at 5 min). A pipeline window
        # shorter than that cannot host the EMA; any(flags) on it labelled
        # ordinary steps as bias_offset. When the series is long enough,
        # only the last timestep votes.
        bias_ema_alarmed: list[str] = []
        if _denormed_for_noise.shape[0] >= BIAS_EMA_SPAN:
            for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
                bias_flags_ema = BiasEMADetector.detect_ema(
                    _denormed_for_noise[:, s_idx],
                    threshold=BIAS_EMA_THRESHOLD_BY_SENSOR.get(
                        s_name, BIAS_EMA_THRESHOLD
                    ),
                )
                if bias_flags_ema and bias_flags_ema[-1]:
                    bias_ema_alarmed.append(s_name)
        bias_ema_flag = len(bias_ema_alarmed) > 0
        if bias_ema_flag:
            logger.info(
                "Bias (EMA) detected for tenant %s — sensors: %s",
                customer_id,
                bias_ema_alarmed,
            )

        # 0e. Dual-sensor checks — bias cross-difference + correlation breakdown.
        _dual = dual_sensor_votes(
            _denormed_for_noise, _vote_context, _profile, _window_state
        )
        for _dual_key, _dual_variables in _dual.votes.items():
            for _dual_variable in _dual_variables:
                logger.warning(
                    "%s on %s for tenant %s",
                    "Dual-sensor bias"
                    if _dual_key == "bias_dual"
                    else "Correlation breakdown",
                    _dual_variable,
                    customer_id,
                )
            rule_votes[_dual_key] = list(_dual_variables)

        # 0f. Diurnal profile check — hourly Z-score against historical stats.
        # Profiles auto-build from the tenant's stored history on first use.
        # Light is excluded (diurnal ramp and night zeros are natural signal).
        await _history.load_diurnal_profiles(db, seq_ts)
        _window_state = window_state(_vote_context)
        _diurnal = diurnal_vote(
            _denormed_for_noise, _vote_context, _profile, _window_state
        )
        if _diurnal.fired:
            logger.warning(
                "Diurnal profile deviation for tenant %s — sensors: %s",
                customer_id, list(_diurnal.votes["diurnal"]),
            )
            rule_votes["diurnal"] = list(_diurnal.votes["diurnal"])

        # 0g. Multivariate joint-implausibility (rolling Mahalanobis) — catches
        # in-range readings that are jointly impossible (plan P3). The last
        # timestep is the current reading under evaluation. Enough stored
        # history fits the covariance; a short or missing history keeps the
        # window fit and still votes on the last step only.
        #
        # NON-VOTING (2026-11-15): history-based path fired on 9/167 clean windows
        # and added zero unique detections. Vote suppressed until the reference
        # distribution is conditioned on time-of-day and the cutoff is taken from
        # the history's own distance distribution rather than a fixed chi-squared
        # table.  Controlled by _MAHALANOBIS_HISTORY_VOTE_ENABLED (False by default).
        # When disabled, neither the DB query nor the covariance fit runs.
        if _MAHALANOBIS_HISTORY_VOTE_ENABLED:
            history = await self._load_mahalanobis_history(
                customer_id,
                greenhouse_id,
                db,
                before=history_before,
            )
            n_sensors = int(_denormed_for_noise.shape[1])
            history_ready = (
                history.ndim == 2
                and history.shape[1] == n_sensors
                and int(history.shape[0]) >= 2 * n_sensors
            )
            if history_ready:
                mahal_last = self._mahalanobis_detector.score_last_against_history(
                    _denormed_for_noise,
                    history,
                )
            else:
                mahal_flags = self._mahalanobis_detector.detect(_denormed_for_noise)
                mahal_last = bool(mahal_flags and mahal_flags[-1])
            if mahal_last:
                logger.warning(
                    "Multivariate implausibility (non-voting) for tenant %s at current reading",
                    customer_id,
                )
                # rule_votes["multivariate"] intentionally omitted — non-voting.

        # 0h. Change-point (BOCPD) — level shifts CUSUM's fixed K/H may miss.
        # Confirmed anywhere in the window on BOCPD_MIN_SENSORS+ channels,
        # light excluded (the core owns the rule).
        _change_point = change_point_vote(
            _denormed_for_noise, _vote_context, _profile, _window_state
        )
        if _change_point.fired:
            logger.warning(
                "Change-point detected for tenant %s — sensors: %s",
                customer_id, list(_change_point.votes["change_point"]),
            )
            rule_votes["change_point"] = list(_change_point.votes["change_point"])

        rule_votes = order_rule_votes(rule_votes)

        # 1. LSTM-VAE forward pass.
        _raw_score, anomaly_score, embedding, uncertainty = self._run_lstm_vae(seq_array)

        # 1a. CUSUM drift update — physical EMA residuals, load before update.
        # A fresh pipeline (live enrichment builds one per reading) has an
        # empty cache, so the saved LearningState must be installed before
        # any detector is constructed. setdefault-before-load skips that
        # state. The first sample on an empty EMA returns residual 0.
        # Readings are the denormalized last timestep. Floors come from
        # EMABaselineEstimator.for_physical_series, not from this caller.
        # update_and_reset is edge-triggered: a True return is the alarm,
        # and the accumulators are already clear, so alarmed_sensors() is
        # empty afterwards. Step 5a is the only vote; CUSUM is not a rule.
        cusum_drift_alarm = False
        cusum_alarmed_sensors: list[str] = []
        await self._restore_cusum_ema(customer_id, greenhouse_id, db)
        _cusum_key = (customer_id, greenhouse_id)
        _cusum = self._cusum_cache.get(_cusum_key)
        if _cusum is None:
            _cusum = CUSUMDriftDetector(k=CUSUM_K_SIGMA, h=CUSUM_H_SIGMA)
            self._cusum_cache[_cusum_key] = _cusum
        # Mutate H only (never rebuild) so LearningState round-trips stay valid.
        # Use mean factor across sensors as a single decision interval scale.
        _cusum_factors = [
            _window_state.scaler.factor(_season_key, s) for s in SENSOR_NAMES[: seq_array.shape[1]]
        ]
        _cusum.h = CUSUM_H_SIGMA * (
            sum(_cusum_factors) / len(_cusum_factors) if _cusum_factors else 1.0
        )
        _ema = self._ema_cache.get(_cusum_key)
        if _ema is None:
            _ema = EMABaselineEstimator.for_physical_series()
            self._ema_cache[_cusum_key] = _ema
        _physical_last = _denormed_for_noise[-1]
        for _idx, _sensor in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            _residual, _ = _ema.update(_sensor, float(_physical_last[_idx]))
            if _cusum.update_and_reset(_sensor, _residual):
                cusum_drift_alarm = True
                cusum_alarmed_sensors.append(_sensor)
        if cusum_drift_alarm:
            logger.info(
                "CUSUM drift alarm for tenant %s — sensors: %s",
                customer_id,
                cusum_alarmed_sensors,
            )

        # 2. Physics validation — may short-circuit the pipeline. Suppression
        # only quiets the model signal: a fired rule detector is direct
        # evidence of a sensor fault, so rule votes survive suppression
        # (pre-fusion, gates returned before physics ever ran).
        physics_result = self._run_physics_check(
            seq_array, _as_utc(seq_ts), climate_context
        )
        if physics_result.suppressed:
            if rule_votes:
                logger.info(
                    "Physics suppression (%s) overridden by rule votes %s for tenant %s",
                    physics_result.reason,
                    sorted(rule_votes),
                    customer_id,
                )
            else:
                logger.info(
                    "Physically suppressed anomaly for tenant %s: %s",
                    customer_id,
                    physics_result.reason,
                )
                return self._suppressed_result(
                    anomaly_score=anomaly_score, reason=physics_result.reason
                )
        if physics_result.confirmed:
            logger.info(
                "Physics confirmed anomaly for tenant %s: %s -> %s",
                customer_id,
                physics_result.reason,
                _PHYSICS_REASON_TO_FAULT.get(physics_result.reason, "spike_fault"),
            )

        # 3. Attribution + pattern detection.
        attribution_result, pattern_result = self._run_attribution_pattern(
            seq_array, attributor, pattern_detector
        )
        pattern_type = str(pattern_result["primary_pattern"])

        # 4. EWMA/POT: load adaptive threshold from DB; update after scoring.
        # The core resolves the cutoff: max(EWMA, POT) when finite, else the
        # profile's fallback (a failed tracker load keeps the fallback).
        _lstm_cutoff = lstm_threshold(_profile)
        _ewma_tracker = _pot_fitter = None
        try:
            _ewma_tracker, _pot_fitter = await self._load_trackers(customer_id, greenhouse_id, db)
            _lstm_cutoff = lstm_threshold(
                _profile,
                tracker_thresholds=(
                    _ewma_tracker.get_threshold(),
                    _pot_fitter.get_threshold(),
                ),
            )
        except Exception:
            logger.warning("EWMA/POT tracker load skipped (non-critical)", exc_info=True)
        _effective_threshold = _lstm_cutoff.value

        # 5. Ensemble scores (non-voting, 2026-11-16) + fault classification.
        # detect() still runs; is_anomaly / fault_type do not come from it.
        is_anomaly, fault_type, ensemble_result = self._run_ensemble_classify(
            seq_array=seq_array,
            baseline=baseline,
            anomaly_score=anomaly_score,
            effective_threshold=_effective_threshold,
            pattern_type=pattern_type,
            physics_result=physics_result,
        )

        # 5a. CUSUM drift — non-voting (demoted 2026-11-14).
        # Measured at K=0.25, H=4: in-control run length is ~35-41 samples on
        # stationary noise (20 runs of 5,000 samples), i.e. a false alarm
        # roughly every 3 hours per sensor at 5-minute sampling — far short of
        # the ARL0 ~ 340 the docs previously claimed.  CUSUM state is still
        # computed, logged, and persisted (via LearningState) so the
        # accumulator survives across calls, but it no longer sets is_anomaly
        # or fault_type.  It will be re-armed once the detector is rebuilt
        # against an hour-of-day reference with thresholds chosen from a
        # measured false-alarm target.
        if cusum_drift_alarm and not physics_result.confirmed:
            # Non-voting: alarm is logged above; do not promote is_anomaly
            # or override fault_type until the detector is rebuilt.
            pass

        # 5b. Bias EMA: overrides fault_type when EMA-based bias was detected.
        if bias_ema_flag and is_anomaly and not physics_result.confirmed:
            fault_type = "bias_offset"

        # 5c. Rule-detector votes confirm the anomaly. The highest-precedence
        # fired rule (RULE_PRECEDENCE, applied via order_rule_votes) takes
        # fault-type precedence unless physics already confirmed a fault.
        # Short-circuits (dropout, OOB) returned before this point. Bands
        # are applied later, and only when no rule voted.
        _rule_decision = apply_rule_votes(
            rule_votes,
            is_anomaly=is_anomaly,
            fault_type=fault_type,
            physics_confirmed=physics_result.confirmed,
        )
        is_anomaly = _rule_decision.is_anomaly
        fault_type = _rule_decision.fault_type
        rule_profile: Optional[Mapping[str, str]] = _rule_decision.profile

        # 5c-gate. Sensor-trust gate: a band value measured by a sensor that a
        # sensor-fault detector flagged is untrustworthy — discard the finding.
        # Runs here because rule_votes is now final (all sensor-fault detectors
        # voted during the detection loop; steps 5a/5b set is_anomaly but do
        # not add to rule_votes).
        _fault_sensors: set[str] = {
            s for sensors in rule_votes.values() for s in sensors
        }
        if band_findings and _fault_sensors:
            _trusted_band: list[dict[str, Any]] = []
            _dropped_ids: set[int] = set()
            for _bf in band_findings:
                _bf_sensors = set(_bf.get("affected_sensors") or [])
                if _bf_sensors & _fault_sensors:
                    logger.info(
                        "Band finding %s dropped for tenant %s: sensors %s "
                        "overlap fault-sensor set %s",
                        _bf.get("anomaly_type"),
                        customer_id,
                        sorted(_bf_sensors),
                        sorted(_fault_sensors),
                    )
                    _dropped_ids.add(id(_bf))
                else:
                    _trusted_band.append(_bf)
            if _dropped_ids:
                scada_anomalies[:] = [
                    f for f in scada_anomalies if id(f) not in _dropped_ids
                ]
            band_findings = _trusted_band

        # 5c-last. Band-breach last resort: a configured-band breach is a
        # categorical anomaly per product spec, but only after all higher-
        # precedence detectors have had their say and found nothing.
        # Conditions (ALL must hold): the greenhouse has stored operational
        # overrides (an empty override dict is the default tomato profile,
        # and a summer afternoon above that profile is not a sensor fault),
        # physics did not suppress, no sensor-fault rule fired, and the
        # LSTM path did not flag. Ensemble and CUSUM are non-voting and
        # do not set is_anomaly.
        # The finding stays on scada_anomalies either way.
        # BAND_TYPE_TO_VOTE provides the fault_type lookup; the profile is set
        # on rule_profile so the root-cause block (step 8) picks up the band's
        # severity/urgency and the reasoning is drawn from scada_anomalies.
        _band_profile = band_promotion(
            band_findings,
            overrides_configured=_band_overrides_configured,
            physics_suppressed=physics_result.suppressed,
            rule_votes=rule_votes,
            is_anomaly=is_anomaly,
        )
        if _band_profile:
            is_anomaly = True
            fault_type = _band_profile["fault_type"]
            rule_profile = _band_profile

        # 5d. Psychrometric consistency suppression — when the measured HD
        # cross-checks cleanly and the ML anomaly's primary implicated
        # channel is humidity, downgrade to suppressed (same seam as
        # PhysicsValidator diurnal suppression). Rule votes survive.
        if (
            physics_result.reason == "psychrometric_consistent"
            and is_anomaly
            and not rule_votes
            and "humidity" in attribution_result.get("affected_sensors", [])
        ):
            logger.info(
                "Psychrometric-consistent suppression for tenant %s: "
                "humidity-implicated anomaly downgraded",
                customer_id,
            )
            return self._suppressed_result(
                anomaly_score=anomaly_score,
                reason="psychrometric_consistent",
            )

        # 5e. Non-anomalous result — clear fault label and fault-specific diagnosis.
        # fault_type is computed unconditionally in _run_ensemble_classify from
        # FaultClassifier.classify(pattern_type), which maps "saturation" and "drift"
        # to "stuck_fault" even for clean windows (PatternDetector scores daytime
        # light railing at ~1.0 normalized as saturation). When no detector, rule,
        # or physics check confirmed an anomaly, the label is a pipeline artifact
        # that must not appear in the response or any downstream consumer.
        if not is_anomaly:
            fault_type = None

        # 6. Persist updated tracker scores.
        try:
            if _ewma_tracker is not None and _pot_fitter is not None:
                _ewma_tracker.update(anomaly_score)
                _pot_fitter.update(anomaly_score)
                await self._save_trackers(customer_id, greenhouse_id, _ewma_tracker, _pot_fitter, db)
        except Exception:
            logger.warning("EWMA/POT tracker update skipped (non-critical)", exc_info=True)

        # 7. Confidence scoring.
        confidence = self.confidence_scorer.score(
            reconstruction_error=anomaly_score,
            pattern_scores=pattern_result["pattern_scores"],
            ensemble_agreement=ensemble_result.agreement_count,
            uncertainty=uncertainty,
        )
        confidence_level = self.confidence_scorer.confidence_level(confidence)

        # 8. Root cause + recommendations. A fired rule pins the root cause,
        # severity, urgency, AND reasoning (the analyzer's pattern-based text
        # describes the model, not the algorithm that actually flagged the
        # fault), so the explanation always matches the detection method.
        root_cause_result = self.root_cause_analyzer.analyze(
            pattern_type=pattern_type,
            attribution=attribution_result["attribution"],
            sequence=seq_array,
            # Reasoning text quotes readings, so it needs °C / %RH / ppm / lux —
            # seq_array is normalized and reads as "Humidity at 1.0%".
            physical_sequence=_denormed_for_noise,
        )
        if rule_profile is not None:
            root_cause_result = {
                **root_cause_result,
                "root_cause": rule_profile["root_cause"],
                "severity": rule_profile["severity"],
                "urgency": rule_profile["urgency"],
                "reasoning": rule_reasoning(rule_votes),
            }
            # Setpoint prose only when that rule won precedence. A co-firing
            # flatline must not replace a spike or change-point explanation.
            if scada_anomalies and primary_rule_is_setpoint(rule_votes):
                root_cause_result["reasoning"] = scada_anomalies[0]["reasoning"]
        # Non-anomalous results carry no fault-specific root cause, severity,
        # urgency, or reasoning. The pattern-based analysis is only meaningful
        # when an anomaly has been confirmed; for clean windows PatternDetector
        # scores can produce misleading diagnosis labels (e.g. "saturation" on
        # daytime light railing → root_cause="sensor_saturation_or_clamping").
        if not is_anomaly:
            root_cause_result = {
                **root_cause_result,
                "root_cause": "normal",
                "severity": "low",
                "urgency": "normal",
                "reasoning": "",
                "alternative_causes": [],
            }
        recommendation_result = self.recommendation_engine.recommend(
            root_cause=root_cause_result["root_cause"],
            severity=root_cause_result.get("severity", "medium"),
        )
        reasoning = root_cause_result.get("reasoning", "")

        # Merge rule-vote evidence into the reported detector scores and
        # affected sensors. The winning rule's sensors lead (precedence
        # order), then co-firing rules, then the model's attribution.
        detector_scores = dict(ensemble_result.detector_scores)
        affected_sensors = list(attribution_result["affected_sensors"])
        if rule_votes:
            ordered_sensors: list[str] = []
            for rule_name, rule_sensors in rule_votes.items():
                detector_scores[rule_name] = 1.0
                for sensor in rule_sensors:
                    if sensor not in ordered_sensors:
                        ordered_sensors.append(sensor)
            affected_sensors = [
                *ordered_sensors,
                *(s for s in affected_sensors if s not in ordered_sensors),
            ]

        root_cause_details = {
            "attribution": attribution_result["attribution"],
            "affected_sensors": affected_sensors,
            "pattern_type": pattern_type,
            "pattern_explanation": pattern_result.get("explanation", ""),
            # Physical units: this is operator-facing (°C, %RH, ppm, lux),
            # and every documented consumer reads it as such.
            "sensor_values_at_peak": self._peak_sensor_values(_denormed_for_noise),
            "detector_scores": detector_scores,
            "ensemble_score": ensemble_result.ensemble_score,
            "ensemble_agreement": ensemble_result.agreement_count,
            "fault_type": fault_type,
            "physics_validation": physics_result.reason,
            "rule_votes": rule_votes,
            "scada_anomalies": scada_anomalies,
            # Persisted so the feedback UI can show the explanation; the fused
            # path previously omitted both, leaving the review queue blank.
            "reasoning": reasoning,
            "hint_summary": reasoning[:120],
        }

        # 9. Incident storage + online learner observation.
        incident_id = await self._store_and_observe(
            customer_id=customer_id,
            greenhouse_id=greenhouse_id,
            is_anomaly=is_anomaly,
            embedding=embedding,
            anomaly_score=anomaly_score,
            sequence_raw=sequence_raw,
            uncertainty=uncertainty,
            root_cause_result=root_cause_result,
            root_cause_details=root_cause_details,
            fault_type=fault_type,
            db=db,
        )

        learning_progress = self._learning_progress(customer_id)

        if self.metrics is not None:
            self.metrics.record_detection(
                anomaly_score=anomaly_score,
                confidence=confidence,
                pattern_type=pattern_type,
                customer_id=customer_id,
            )

        return PipelineResult(
            is_anomaly=is_anomaly,
            anomaly_score=anomaly_score,
            confidence=confidence,
            confidence_level=confidence_level,
            pattern_type=pattern_type,
            fault_type=fault_type,
            attribution=attribution_result["attribution"],
            affected_sensors=affected_sensors,
            pattern_scores=pattern_result["pattern_scores"],
            root_cause=root_cause_result["root_cause"],
            root_cause_confidence=root_cause_result.get("confidence", 0.5),
            reasoning=reasoning,
            alternative_causes=root_cause_result.get("alternative_causes", []),
            severity=root_cause_result.get("severity", "medium"),
            urgency=root_cause_result.get("urgency", "normal"),
            immediate_actions=recommendation_result.get("immediate_actions", []),
            follow_up_actions=recommendation_result.get("follow_up_actions", []),
            all_recommendations=recommendation_result.get("all_actions", []),
            requires_technician=recommendation_result.get("requires_technician", False),
            estimated_resolution_minutes=recommendation_result.get("estimated_downtime_minutes", 30),
            incident_id=incident_id,
            detector_scores=detector_scores,
            ensemble_score=ensemble_result.ensemble_score,
            ensemble_agreement=ensemble_result.agreement_count,
            physics_validation=physics_result.reason,
            learning_progress=learning_progress,
            timestamp=now.isoformat(),
            pipeline_version=PIPELINE_VERSION,
            scada_anomalies=scada_anomalies,
        )

    @staticmethod
    def _suppressed_result(
        anomaly_score: float,
        reason: str,
    ) -> "PipelineResult":
        """Minimal result dict for a physically suppressed anomaly."""
        return PipelineResult(
            is_anomaly=False,
            anomaly_score=anomaly_score,
            confidence=0.0,
            confidence_level="very_low",
            pattern_type="normal",
            fault_type=None,
            attribution={},
            affected_sensors=[],
            pattern_scores={},
            root_cause="physically_suppressed",
            root_cause_confidence=0.0,
            reasoning=f"Suppressed by physics validator: {reason}",
            alternative_causes=[],
            severity="low",
            urgency="normal",
            immediate_actions=[],
            follow_up_actions=[],
            all_recommendations=[],
            requires_technician=False,
            estimated_resolution_minutes=0,
            incident_id=None,
            detector_scores={},
            ensemble_score=0.0,
            ensemble_agreement=0,
            physics_validation=reason,
            learning_progress=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pipeline_version=PIPELINE_VERSION,
            scada_anomalies=[],
        )

    @staticmethod
    def _dropout_result(reason: str) -> "PipelineResult":
        """Result for a confirmed polling dropout event.

        Returned before the LSTM-VAE forward pass, so no anomaly_score is
        available; 1.0 is used as the maximum sentinel value to signal a
        confirmed fault condition.
        """
        return PipelineResult(
            is_anomaly=True,
            anomaly_score=1.0,
            confidence=1.0,
            confidence_level="very_high",
            pattern_type="polling_dropout",
            fault_type="polling_dropout",
            attribution={},
            affected_sensors=list(SENSOR_NAMES),
            pattern_scores={},
            root_cause="polling_dropout",
            root_cause_confidence=1.0,
            reasoning=reason,
            alternative_causes=[],
            severity="high",
            urgency="urgent",
            immediate_actions=["check_sensor_connectivity", "inspect_data_pipeline"],
            follow_up_actions=["review_network_logs"],
            all_recommendations=[
                "check_sensor_connectivity",
                "inspect_data_pipeline",
                "review_network_logs",
            ],
            requires_technician=True,
            estimated_resolution_minutes=30,
            incident_id=None,
            detector_scores={},
            ensemble_score=0.0,
            ensemble_agreement=0,
            physics_validation=None,
            learning_progress=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pipeline_version=PIPELINE_VERSION,
            scada_anomalies=[],
        )

    @staticmethod
    def _build_oob_result(alarmed_sensors: List[str]) -> "PipelineResult":
        """Result for an out-of-bounds hard-limit violation."""
        reason = (
            "Out-of-bounds sensor readings on: "
            + ", ".join(alarmed_sensors)
        )
        return PipelineResult(
            is_anomaly=True,
            anomaly_score=1.0,
            confidence=1.0,
            confidence_level="very_high",
            pattern_type="spike",
            fault_type="out_of_bounds",
            attribution={},
            affected_sensors=alarmed_sensors,
            pattern_scores={},
            root_cause="sensor_out_of_bounds",
            root_cause_confidence=1.0,
            reasoning=reason,
            alternative_causes=["sensor_hardware_failure", "wiring_fault"],
            severity="critical",
            urgency="immediate",
            immediate_actions=["isolate_sensor", "check_wiring", "inspect_sensor_hardware"],
            follow_up_actions=["replace_sensor", "recalibrate"],
            all_recommendations=[
                "isolate_sensor", "check_wiring",
                "inspect_sensor_hardware", "replace_sensor",
            ],
            requires_technician=True,
            estimated_resolution_minutes=30,
            incident_id=None,
            detector_scores={},
            ensemble_score=0.0,
            ensemble_agreement=0,
            physics_validation=None,
            learning_progress=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pipeline_version=PIPELINE_VERSION,
            scada_anomalies=[],
        )

    async def _get_seasonal_scaler(
        self,
        tenant_id: str,
        greenhouse_id: str,
        season: str,
        op_config: dict[str, Any],
        db: AsyncSession,
    ) -> SeasonalScaler:
        """Return a cached SeasonalScaler (see ``scoring_history``)."""
        return await load_seasonal_scaler(
            db, tenant_id, greenhouse_id, season, op_config
        )

    async def _load_mahalanobis_history(
        self,
        customer_id: str,
        greenhouse_id: str,
        db: AsyncSession,
        *,
        before: Optional[datetime],
    ) -> np.ndarray:
        """Cached finite physical history for the Mahalanobis covariance fit.

        Keyed by tenant and greenhouse. Live enrichment builds a new pipeline
        per reading, so the hit is the second lookup inside one ``run``.
        A query failure is logged and returns an empty array; the vote then
        stays on ``detect()``.
        """
        key = (customer_id, greenhouse_id)
        cached = self._mahalanobis_history_cache.get(key)
        if cached is not None:
            return cached
        try:
            history = await _query_physical_history(
                db,
                customer_id=customer_id,
                greenhouse_id=greenhouse_id,
                before=before,
            )
        except Exception:
            logger.warning(
                "Mahalanobis history load failed for tenant %s greenhouse %s "
                "(non-critical)",
                customer_id,
                greenhouse_id,
                exc_info=True,
            )
            await _rollback_quietly(db, customer_id, greenhouse_id)
            history = np.empty((0, len(SENSOR_NAMES)), dtype=float)
        self._mahalanobis_history_cache[key] = history
        return history

    async def _ensure_diurnal_profiles(
        self,
        customer_id: str,
        greenhouse_id: str,
        db: AsyncSession,
        *,
        anchor_ts: Optional[datetime] = None,
    ) -> Dict[str, Dict[int, tuple]]:
        """Cached diurnal profiles (see ``scoring_history.load_diurnal_profiles``)."""
        anchor = anchor_ts if anchor_ts is not None else datetime.now(timezone.utc)
        return await load_diurnal_profiles(
            db,
            customer_id,
            greenhouse_id,
            anchor,
            self._diurnal_profiles_cache,
        )

    def _evict_cusum_ema_caches(self, customer_id: str) -> None:
        """Remove all CUSUM and EMA cache entries for the evicted tenant."""
        for cache in (self._cusum_cache, self._ema_cache):
            to_remove = [k for k in cache if k[0] == customer_id]
            for k in to_remove:
                del cache[k]

    def _evict_diurnal_cache(self, customer_id: str) -> None:
        """Remove all diurnal cache entries for the evicted tenant."""
        to_remove = [
            key for key in self._diurnal_profiles_cache
            if key[0] == customer_id
        ]
        for key in to_remove:
            del self._diurnal_profiles_cache[key]

    # ── Private helpers extracted from _run_with_components ───────────────────

    def _run_lstm_vae(
        self, seq_array: np.ndarray
    ) -> tuple[float, float, list, float]:
        """LSTM-VAE forward pass.

        Returns:
            (raw_score, anomaly_score, embedding, uncertainty)
        """
        raw_scores, embeddings, uncertainties = (
            self.detector.predict_batch_enriched([seq_array])
        )
        # Same forward pass as ``ModelScorer`` (A3 proves them bit-equal);
        # it stays behind ``detector.predict_batch_enriched`` because that is
        # the seam the pipeline tests substitute. The scale is the core's.
        scores = ModelScores(
            raw=raw_scores,
            per_channel=np.empty((len(raw_scores), 0), dtype=np.float32),
            embeddings=embeddings,
            uncertainties=uncertainties,
        )
        raw_score = float(raw_scores[0])
        anomaly_score = scaled_scores(scores, self._profile)[0]
        embedding: list = embeddings[0].tolist()
        uncertainty = float(uncertainties[0])
        return raw_score, anomaly_score, embedding, uncertainty

    def _run_physics_check(
        self,
        seq_array: np.ndarray,
        now: datetime,
        climate_context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Denormalize sequence and run PhysicsValidator.validate().

        When *climate_context* carries a ``measured_hd`` value, the last
        timestep's denormalized temperature and humidity are cross-checked
        against the measured humidity deficit via
        :func:`~src.models.psychrometric_validator.validate_row`.
        A ``FAILED_PSYCHROMETRIC_LIMIT`` result overrides any trend-based
        outcome with ``confirmed=True, reason='psychrometric_inconsistency'``.
        """
        from src.models.physics_validator import ValidationResult as _VR

        denormed = _denormalize_sequence(seq_array)
        result = PhysicsValidator.validate(denormed, now)

        # --- Psychrometric cross-check (opt-in, requires measured_hd) --------
        if climate_context:
            hd_measured = climate_context.get("measured_hd")
            if hd_measured is not None:
                try:
                    hd_val = float(hd_measured)
                except (TypeError, ValueError):
                    hd_val = None
                if hd_val is not None:
                    last_step = denormed[-1]
                    t_air = float(last_step[0])   # temperature
                    rh = float(last_step[1])       # humidity
                    try:
                        status = psychrometric_validate_row(t_air, rh, hd_val)
                    except ValueError:
                        # Unphysical inputs — skip psychrometric overlay.
                        status = "OK"
                    if status == "FAILED_PSYCHROMETRIC_LIMIT":
                        return _VR(
                            suppressed=False,
                            confirmed=True,
                            reason="psychrometric_inconsistency",
                        )
                    # Consistent HD is recorded on the result so the caller
                    # can suppress humidity-implicated ML anomalies later.
                    if not result.confirmed and not result.suppressed:
                        return _VR(
                            suppressed=False,
                            confirmed=False,
                            reason="psychrometric_consistent",
                        )

        return result

    def _run_attribution_pattern(
        self,
        seq_array: np.ndarray,
        attributor: AnomalyAttributor,
        pattern_detector: PatternDetector,
    ) -> tuple[dict, dict]:
        """Run attribution and pattern detection.

        Returns:
            (attribution_result, pattern_result)
        """
        attribution_result = attributor.attribute_sequence(seq_array)
        pattern_result = pattern_detector.detect(seq_array)
        return attribution_result, pattern_result

    def _run_ensemble_classify(
        self,
        seq_array: np.ndarray,
        baseline: np.ndarray,
        anomaly_score: float,
        effective_threshold: float,
        pattern_type: str,
        physics_result: Any,
    ) -> tuple[bool, str, Any]:
        """Run ensemble detection and fault classification.

        The ensemble score is computed for the response. It does not vote:
        ``is_anomaly`` here is the LSTM threshold and/or physics confirmation,
        and ``fault_type`` comes from physics or ``FaultClassifier``. Stuck
        labels stay with the setpoint flatline rule vote.

        Returns:
            (is_anomaly, fault_type, ensemble_result)
        """
        ensemble = self._get_ensemble()
        ensemble_result = ensemble.detect(seq_array, baseline)
        is_lstm_anomaly = lstm_flag(anomaly_score, effective_threshold, self._profile)
        physics_confirmed = physics_result.confirmed

        # NON-VOTING (2026-11-16): measured with a real tenant baseline, the
        # ensemble flagged 19 of 167 clean July windows and added zero
        # detections the other rules did not already make. Scores and
        # agreement stay on the response. ensemble_result.is_anomaly does
        # not promote is_anomaly, and ensemble_result.fault_type does not
        # set the label. Stuck already votes via the setpoint flatline rule
        # (sensor_flatline → stuck_fault), so the ensemble stuck pre-check
        # keeps no label exception.
        is_anomaly = physics_confirmed or is_lstm_anomaly

        if physics_confirmed:
            fault_type: str = _PHYSICS_REASON_TO_FAULT.get(
                physics_result.reason, "spike_fault"
            )
        else:
            fault_type = FaultClassifier.classify(
                pattern_type=pattern_type,
                sensor_stats={},
                bias_flag=ensemble_result.bias_flag,
            )
        return is_anomaly, fault_type, ensemble_result

    async def _persist_gate_result(
        self,
        result: "PipelineResult",
        *,
        customer_id: str,
        greenhouse_id: str,
        seq_array: np.ndarray,
        sequence_raw: List[List[float]],
        db: AsyncSession,
    ) -> "PipelineResult":
        """Persist an incident for a hard short-circuit detection (dropout/OOB).

        These gates fire before the LSTM forward pass, so the embedding is
        computed here on demand — they fire rarely, so the extra pass is
        negligible. Persistence failing must never block the detection
        result: the operator still needs the alarm even if the incident
        row could not be written. (Soft rule detectors fuse into the main
        pipeline via ``rule_votes`` and persist through the normal path.)
        """
        embedding: list = []
        uncertainty = 0.0
        # Dropout sequences may carry NaNs the model can't embed.
        if np.isfinite(seq_array).all():
            try:
                _, _, embedding, uncertainty = self._run_lstm_vae(seq_array)
            except Exception:
                logger.warning(
                    "Gate incident embedding failed (non-critical)", exc_info=True
                )
        root_cause_details = {
            "attribution": result["attribution"],
            "affected_sensors": result["affected_sensors"],
            "pattern_type": result["pattern_type"],
            "fault_type": result["fault_type"],
            "detector_scores": result["detector_scores"],
            "reasoning": result["reasoning"],
        }
        try:
            incident_id = await self._store_and_observe(
                customer_id=customer_id,
                greenhouse_id=greenhouse_id,
                is_anomaly=result["is_anomaly"],
                embedding=embedding,
                anomaly_score=result["anomaly_score"],
                sequence_raw=sequence_raw,
                uncertainty=uncertainty,
                root_cause_result={"root_cause": result["root_cause"]},
                root_cause_details=root_cause_details,
                fault_type=result["fault_type"],
                db=db,
            )
            result["incident_id"] = incident_id
        except Exception:
            logger.exception("Gate incident persistence failed (non-critical)")
        if self.metrics is not None:
            self.metrics.record_detection(
                anomaly_score=result["anomaly_score"],
                confidence=result["confidence"],
                pattern_type=result["pattern_type"],
                customer_id=customer_id,
            )
        return result

    async def _store_and_observe(
        self,
        customer_id: str,
        greenhouse_id: str,
        is_anomaly: bool,
        embedding: list,
        anomaly_score: float,
        sequence_raw: List[List[float]],
        uncertainty: float,
        root_cause_result: dict,
        root_cause_details: dict,
        fault_type: Optional[str],
        db: AsyncSession,
    ) -> Optional[str]:
        """Store incident in DB and feed the online learner (anomalies only).

        Returns:
            The new incident_id, or None when is_anomaly is False.
        """
        memory = IncidentMemory(db)
        incident_id: Optional[str] = None
        if is_anomaly:
            incident_id = await memory.store_incident(
                tenant_id=customer_id,
                greenhouse_id=greenhouse_id,
                embedding=embedding,
                anomaly_score=anomaly_score,
                sequence=sequence_raw,
                embedding_uncertainty=uncertainty,
                root_cause=root_cause_result["root_cause"],
                root_cause_details=root_cause_details,
                tags=["pipeline_detected"],
                fault_type=fault_type,
            )
            # Wire into per-tenant online learner so it continuously adapts to
            # this tenant's real sensor distribution.
            try:
                _observe_online_learner(
                    customer_id=customer_id,
                    sequence=np.asarray(sequence_raw, dtype=np.float32),
                    anomaly_score=anomaly_score,
                )
            except Exception:
                logger.exception("Online learner observe failed (non-critical)")
        return incident_id

    # ── EWMA / POT tracker persistence ────────────────────────────────────────

    def _install_saved_cusum_ema(
        self,
        customer_id: str,
        greenhouse_id: str,
        state: Dict[str, Any],
    ) -> None:
        """Install persisted CUSUM and EMA into empty cache slots only.

        A slot that already holds a detector is the in-process state for
        this tenant/greenhouse, including an update that has not been saved
        yet.  Replacing it with the row just read would drop that update.

        LearningState is keyed at tenant level (one row per tenant), so the
        same persisted blob is restored into whichever (tenant, greenhouse)
        key requests it first.  In-memory state is keyed per-(tenant,
        greenhouse) for isolation; persistence stays per-tenant.

        Legacy EMA payloads (written when the pipeline fed normalised
        values to the EMA, before ``EMABaselineEstimator.for_physical_series``
        existed) carry an empty ``min_std_by_sensor`` map. Physical
        estimators always serialise a non-empty map. Installing a
        normalised-scale EMA into the physical pipeline produces repeated
        false CUSUM alarms (measured: 37 in 300 clean samples). When the
        marker is absent, neither the EMA nor the CUSUM accumulators are
        restored, so the caller builds fresh physical ones instead.
        """
        ema_data = state.get("ema_baseline_state")
        # Non-empty min_std_by_sensor is the marker that this payload was
        # written by EMABaselineEstimator.for_physical_series(); legacy
        # payloads have an empty or absent map.
        ema_is_physical = (
            ema_data is not None
            and isinstance(ema_data.get("min_std_by_sensor"), dict)
            and bool(ema_data["min_std_by_sensor"])
        )
        _key = (customer_id, greenhouse_id)
        if _key not in self._cusum_cache:
            cusum_data = state.get("cusum_state")
            if cusum_data is not None and ema_is_physical:
                self._cusum_cache[_key] = CUSUMDriftDetector.from_dict(
                    cusum_data
                )
        if _key not in self._ema_cache:
            if ema_data is not None and ema_is_physical:
                self._ema_cache[_key] = EMABaselineEstimator.from_dict(
                    ema_data
                )

    async def _restore_cusum_ema(
        self,
        customer_id: str,
        greenhouse_id: str,
        db: AsyncSession,
    ) -> None:
        """Load ``cusum_state`` and ``ema_baseline_state`` before the update.

        Called while both cache slots are still empty on a fresh pipeline.
        A failure leaves the slots empty so the caller can build a physical
        EMA instead of aborting the reading.
        """
        _key = (customer_id, greenhouse_id)
        if _key in self._cusum_cache and _key in self._ema_cache:
            return
        try:
            result = await db.execute(
                select(LearningState).where(LearningState.customer_id == customer_id)
            )
            row: Optional[LearningState] = result.scalar_one_or_none()
            if row is None:
                return
            self._install_saved_cusum_ema(customer_id, greenhouse_id, row.state_json or {})
        except Exception:
            logger.warning(
                "CUSUM/EMA state load failed for tenant %s (non-critical)",
                customer_id,
                exc_info=True,
            )

    async def _load_trackers(
        self,
        customer_id: str,
        greenhouse_id: str,
        db: AsyncSession,
    ) -> tuple[EWMATracker, POTThresholdFitter]:
        """Load EWMA and POT trackers for a tenant from DB, falling back to cache then fresh."""
        cached = self._tracker_cache.get(customer_id)
        if cached is not None:
            return cached

        try:
            result = await db.execute(
                select(LearningState).where(LearningState.customer_id == customer_id)
            )
            row: Optional[LearningState] = result.scalar_one_or_none()
            if row is not None:
                state = row.state_json or {}
                tracker_data = state.get("ewma_pot_trackers")
                if tracker_data is not None:
                    ewma = EWMATracker.from_dict(tracker_data["ewma"])
                    pot = POTThresholdFitter.from_dict(tracker_data["pot"])
                    # Same empty-slot rule as the pre-update restore. A slot
                    # filled by this call's update must not be replaced by
                    # the state just read, which does not include it yet.
                    self._install_saved_cusum_ema(customer_id, greenhouse_id, state)
                    # Diurnal profiles are keyed by (tenant, greenhouse) but
                    # LearningState is tenant-level and does not record which
                    # greenhouse the profiles were built for. Skip restore here;
                    # _ensure_diurnal_profiles will auto-build per greenhouse on
                    # first access, which is both correct and cached.
                    self._tracker_cache[customer_id] = (ewma, pot)
                    return ewma, pot
        except Exception:
            logger.warning("EWMA/POT DB load failed, using fresh trackers", exc_info=True)

        ewma = EWMATracker(alpha=EWMA_ALPHA, k=EWMA_K)
        pot = POTThresholdFitter(q=POT_Q, fpr=POT_FPR, buffer_size=POT_BUFFER_SIZE)
        self._tracker_cache[customer_id] = (ewma, pot)
        return ewma, pot

    async def _save_trackers(
        self,
        customer_id: str,
        greenhouse_id: str,
        ewma: EWMATracker,
        pot: POTThresholdFitter,
        db: AsyncSession,
    ) -> None:
        """Persist EWMA, POT, and CUSUM trackers for a tenant into LearningState.state_json.

        CUSUM/EMA in-memory caches are keyed by (tenant, greenhouse_id).
        LearningState persistence is per-tenant (one row per tenant), so the
        last greenhouse to call _save_trackers wins in the DB for cusum/ema
        state.  In-memory isolation is fully per-greenhouse; the DB is a
        cold-start warm-up only.
        """
        self._tracker_cache[customer_id] = (ewma, pot)
        _gh_key = (customer_id, greenhouse_id)
        tracker_payload = _json_safe({"ewma": ewma.to_dict(), "pot": pot.to_dict()})
        _cusum = self._cusum_cache.get(_gh_key)
        _ema_bl = self._ema_cache.get(_gh_key)
        try:
            async with db.begin_nested():
                result = await db.execute(
                    select(LearningState).where(LearningState.customer_id == customer_id)
                )
                row: Optional[LearningState] = result.scalar_one_or_none()
                if row is None:
                    initial_state: dict[str, Any] = {
                        "ewma_pot_trackers": tracker_payload
                    }
                    if _cusum is not None:
                        initial_state["cusum_state"] = _json_safe(_cusum.to_dict())
                    if _ema_bl is not None:
                        initial_state["ema_baseline_state"] = _json_safe(_ema_bl.to_dict())
                    db.add(LearningState(
                        customer_id=customer_id,
                        state_json=initial_state,
                    ))
                else:
                    current = dict(row.state_json or {})
                    current["ewma_pot_trackers"] = tracker_payload
                    if _cusum is not None:
                        current["cusum_state"] = _json_safe(_cusum.to_dict())
                    if _ema_bl is not None:
                        current["ema_baseline_state"] = _json_safe(_ema_bl.to_dict())
                    row.state_json = current
            # savepoint auto-commits on clean __aexit__
        except Exception:
            # Preserving "EWMA/POT DB save failed" prefix so existing tests
            # that substring-match this message continue to pass.
            logger.warning("EWMA/POT DB save failed (non-critical)", exc_info=True)
            # savepoint auto-rolls back; outer transaction untouched

    def _learning_progress(
        self, customer_id: str
    ) -> Optional[Dict[str, int]]:
        if customer_id not in self._orchestrators:
            return None
        stats = self._orchestrators[customer_id].learner.get_statistics()
        return {
            "high_quality_feedback": stats["high_quality_feedback"],
            "needed_for_retrain": max(
                0,
                ACTIVE_LEARNING_MIN_QUALITY_LABELS
                - stats["high_quality_feedback"],
            ),
            "retrainings_completed": stats["retrainings_completed"],
        }

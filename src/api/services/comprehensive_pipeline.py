"""
Comprehensive anomaly inference pipeline orchestration.

Wires Detection -> Attribution -> Pattern -> Confidence -> Root Cause ->
Recommendations -> Incident Store -> Learning progress -> Metrics.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TypedDict

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import (
    ACTIVE_LEARNING_MIN_QUALITY_LABELS,
    ANOMALY_SCALING_FACTOR,
    ANOMALY_THRESHOLD,
    BIAS_EMA_THRESHOLD,
    BIAS_EMA_THRESHOLD_BY_SENSOR,
    BOCPD_MIN_SENSORS,
    CUSUM_H_SIGMA,
    CUSUM_K_SIGMA,
    EWMA_ALPHA,
    EWMA_K,
    INPUT_DIM,
    NUMERICAL_EPSILON,
    POT_BUFFER_SIZE,
    POT_FPR,
    POT_Q,
    PSYCHROMETRIC_HD_TOLERANCE,
    SENSOR_NOISE_TARGETS,
    SEQ_LEN,
    SENSOR_NAMES,
    SPIKE_MAD_FLOOR,
    TEMP_RANGE,
    HUMIDITY_RANGE,
    CO2_RANGE,
    LIGHT_RANGE,
)
from src.db.incident_memory import IncidentMemory
from src.db.models import LearningState
from src.models.anomaly_attributor import AnomalyAttributor
from src.models.bias_detector import BiasOffsetDetector as BiasEMADetector
from src.models.bias_dual_sensor_detector import BiasDualSensorDetector
from src.models.bocpd_detector import BOCPDDetector
from src.models.multivariate_mahalanobis_detector import (
    MultivariateMahalanobisDetector,
)
from src.models.band_breach_detector import BAND_TYPE_TO_VOTE, BandBreachDetector
from src.models.confidence_scorer import ConfidenceScorer
from src.models.correlation_breakdown_detector import CorrelationBreakdownDetector
from src.models.data_utils import SENSOR_RANGES
from src.models.cusum_drift_detector import CUSUMDriftDetector, EMABaselineEstimator
from src.models.diurnal_profile_detector import DiurnalProfileDetector
from src.models.dropout_detector import DropoutDetector
from src.models.ensemble_detector import EnsembleAnomalyDetector, LstmVaeDetector
from src.models.noise_detector import NoiseDetector
from src.models.out_of_bounds_detector import OutOfBoundsDetector
from src.models.spike_mad_detector import SpikeMADDetector
from src.models.ewma_tracker import EWMATracker
from src.models.fault_classifier import FaultClassifier
from src.models.inference import AnomalyDetector
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
    build_scaler_from_baselines,
    get_cached_seasonal_scaler,
    put_cached_seasonal_scaler,
)
from src.models.setpoint_scada_detector import (
    DEHUMIDIFICATION_FAILURE,
    HUMIDITY_SATURATION_RISK,
    OVER_HEATING,
    SENSOR_FLATLINE,
    THERMAL_DIVERGENCE,
    UNDER_HEATING,
    SetpointAwareScadaDetector,
    climate_context_from_mapping,
)
from src.monitoring.anomaly_metrics import AnomalyMetrics
from src.utils.lru import LRUDict

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "2.0"


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


def _season_for_timestamp(
    ts: datetime,
    season_map: dict[str, str],
) -> str:
    """Map a timestamp's calendar month onto summer|winter via season_map."""
    month_key = str(int(ts.month))
    season = str(season_map.get(month_key, "winter")).lower()
    return season if season in {"summer", "winter"} else "winter"

# Map PhysicsValidator reason strings to fault type values.
_PHYSICS_REASON_TO_FAULT: Dict[str, str] = {
    "psychrometric_impossibility": "spike_fault",
    "psychrometric_inconsistency": "humidity_fault",
    "nocturnal_light": "spike_fault",
    "co2_out_of_range": "calibration_drift",
    "temperature_out_of_range": "spike_fault",
}

# Fault profile per rule detector (fusion, plan P2). Rule detectors vote
# instead of short-circuiting: the full LSTM/ensemble path still runs, and
# the FIRST fired rule (dict/gate order) takes fault-type precedence over
# model-derived classification. Values mirror the former short-circuit
# result builders so alert semantics stay stable. ``reasoning`` names the
# algorithm that fired ({sensors} is filled with the flagged channels) so
# the explanation always matches the detection method, not the model's
# pattern guess.
_RULE_VOTE_PROFILES: Dict[str, Dict[str, str]] = {
    "noise": {
        "fault_type": "accuracy_decline",
        "root_cause": "sensor_noise_floor_exceeded",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Precision degradation: rolling variance on {sensors} exceeded the "
            "calibrated noise-floor ratio"
        ),
    },
    "spike_mad": {
        "fault_type": "spike_fault",
        "root_cause": "transient_spike",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Transient spike: {sensors} deviated beyond the rolling "
            "median-absolute-deviation (MAD) threshold"
        ),
    },
    "bias_dual": {
        "fault_type": "bias_offset",
        "root_cause": "redundant_sensor_bias_offset",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Dual-sensor bias: cross-difference between redundant {sensors} "
            "sensors exceeded the calibration tolerance"
        ),
    },
    "correlation_breakdown": {
        "fault_type": "correlation_breakdown",
        "root_cause": "spatial_correlation_breakdown",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Spatial correlation breakdown: rolling Pearson correlation on "
            "{sensors} fell below the expected floor"
        ),
    },
    "diurnal": {
        "fault_type": "diurnal_deviation",
        "root_cause": "diurnal_profile_violation",
        "severity": "medium",
        "urgency": "scheduled",
        "reasoning": (
            "Diurnal profile deviation: {sensors} exceeded the historical "
            "hourly Z-score for this hour of day"
        ),
    },
    "multivariate": {
        "fault_type": "accuracy_decline",
        "root_cause": "multivariate_implausibility",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Multivariate implausibility: the joint {sensors} reading exceeded "
            "the Mahalanobis distance cutoff — each channel is in range but "
            "the combination is inconsistent"
        ),
    },
    "change_point": {
        "fault_type": "calibration_drift",
        "root_cause": "level_shift_change_point",
        "severity": "medium",
        "urgency": "scheduled",
        "reasoning": (
            "Change-point: Bayesian online change-point detection (BOCPD) "
            "flagged a level shift on {sensors}"
        ),
    },
    "under_heating": {
        "fault_type": "under_heating",
        "root_cause": "heating_setpoint_deficit",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Under-heating: {sensors} below climate-computer heating setpoint "
            "by more than the configured margin"
        ),
    },
    "over_heating": {
        "fault_type": "over_heating",
        "root_cause": "cooling_or_ventilation_shortfall",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Over-heating: {sensors} above heating setpoint without solar-driven "
            "outdoor conditions to explain the excess"
        ),
    },
    "dehumidification_failure": {
        "fault_type": "dehumidification_failure",
        "root_cause": "dehumidification_capacity_failure",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Dehumidification failure: {sensors} at critical RH with near-zero "
            "humidity deficit"
        ),
    },
    "humidity_saturation_risk": {
        "fault_type": "humidity_saturation_risk",
        "root_cause": "moisture_saturation_near_dewpoint",
        "severity": "medium",
        "urgency": "scheduled",
        "reasoning": (
            "Humidity saturation risk: {sensors} at or above the critical RH "
            "threshold near dew point"
        ),
    },
    "thermal_divergence": {
        "fault_type": "thermal_divergence",
        "root_cause": "crop_air_thermal_disconnect",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Thermal divergence: canopy vs greenhouse air on {sensors} exceeds "
            "the microclimate critical delta"
        ),
    },
    "sensor_flatline": {
        "fault_type": "stuck_fault",
        "root_cause": "sensor_flatline_frozen_telemetry",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": (
            "Sensor flatline: {sensors} frozen (zero variance) for the "
            "configured multi-hour window"
        ),
    },
    "temp_band_high": {
        "fault_type": "temp_band_high",
        "root_cause": "crop_setpoint_band_breach",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": "Temperature above configured crop day/night max on {sensors}",
    },
    "temp_band_low": {
        "fault_type": "temp_band_low",
        "root_cause": "crop_setpoint_band_breach",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": "Temperature below configured crop day/night min on {sensors}",
    },
    "vpd_band_high": {
        "fault_type": "vpd_band_high",
        "root_cause": "crop_setpoint_band_breach",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": "VPD above configured crop day/night max (from {sensors})",
    },
    "vpd_band_low": {
        "fault_type": "vpd_band_low",
        "root_cause": "crop_setpoint_band_breach",
        "severity": "medium",
        "urgency": "scheduled",
        "reasoning": "VPD below configured crop day/night min (from {sensors})",
    },
    "humidity_band_high": {
        "fault_type": "humidity_band_high",
        "root_cause": "crop_setpoint_band_breach",
        "severity": "high",
        "urgency": "urgent",
        "reasoning": "Humidity above configured crop day/night max on {sensors}",
    },
    "humidity_band_low": {
        "fault_type": "humidity_band_low",
        "root_cause": "crop_setpoint_band_breach",
        "severity": "medium",
        "urgency": "scheduled",
        "reasoning": "Humidity below configured crop day/night min on {sensors}",
    },
    "co2_band_high": {
        "fault_type": "co2_band_high",
        "root_cause": "crop_setpoint_band_breach",
        "severity": "medium",
        "urgency": "scheduled",
        "reasoning": "CO₂ above configured crop day/night max on {sensors}",
    },
    "co2_band_low": {
        "fault_type": "co2_band_low",
        "root_cause": "crop_setpoint_band_breach",
        "severity": "medium",
        "urgency": "scheduled",
        "reasoning": "CO₂ below configured crop day/night min on {sensors}",
    },
}

# Sensor ranges in physical units for denormalization.
_DENORM_RANGES = np.array(
    [TEMP_RANGE, HUMIDITY_RANGE, CO2_RANGE, LIGHT_RANGE],
    dtype=np.float32,
)

# Minimum number of stored incidents before enabling dynamic thresholds.
_MIN_INCIDENTS_FOR_DYNAMIC_THRESHOLD: int = 50


def _humanize_sensors(sensors: List[str]) -> str:
    """Readable sensor list for reasoning text ('temperature, humidity')."""
    return ", ".join(s.replace("_", " ") for s in sensors) or "sensor channels"


def build_rule_reasoning(rule_votes: Dict[str, List[str]]) -> str:
    """Human-readable reasoning that names the algorithm(s) that fired.

    Keeps the explanation consistent with the fault type: the primary
    (first-fired) rule leads, and any co-firing rules are appended so the
    operator sees every algorithm that agreed. Empty when no rule fired
    (the caller then keeps the model's pattern-based reasoning).
    """
    if not rule_votes:
        return ""
    items = list(rule_votes.items())
    primary_name, primary_sensors = items[0]
    template = _RULE_VOTE_PROFILES[primary_name]["reasoning"]
    reasoning = template.format(sensors=_humanize_sensors(primary_sensors))
    if len(items) > 1:
        others = ", ".join(
            _RULE_VOTE_PROFILES[name]["root_cause"].replace("_", " ")
            for name, _ in items[1:]
        )
        reasoning = f"{reasoning}; also flagged by: {others}"
    return reasoning


def _denormalize_sequence(normalized: np.ndarray) -> np.ndarray:
    """Convert normalized [0,1] sensor data back to physical units."""
    mins = _DENORM_RANGES[:, 0]
    spans = _DENORM_RANGES[:, 1] - _DENORM_RANGES[:, 0]
    return normalized * spans + mins


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
    ) -> None:
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
            on_evict=lambda customer_id, _: self._cusum_cache.pop(customer_id, None),
        )
        # Polling dropout detector (shared across tenants — stateless).
        self._dropout_detector = DropoutDetector()
        # Noise / precision degradation detector (stateless, shared).
        self._noise_detector = NoiseDetector()
        # Per-tenant CUSUM drift detectors (stateful, persisted in LearningState).
        self._cusum_cache: dict[str, CUSUMDriftDetector] = {}
        # Per-tenant EMA baseline estimators for CUSUM standardized residuals.
        self._ema_cache: dict[str, EMABaselineEstimator] = {}
        # Phase B SCADA detectors (stateless, shared).
        self._spike_mad_detector = SpikeMADDetector()
        self._oob_detector = OutOfBoundsDetector()
        self._diurnal_detector = DiurnalProfileDetector()
        self._bias_dual_detector = BiasDualSensorDetector()
        self._corr_breakdown_detector = CorrelationBreakdownDetector()
        # Sensitivity layer (plan P3): joint-implausibility + change-point,
        # both stateless and shared. They run on the physical-unit window.
        self._mahalanobis_detector = MultivariateMahalanobisDetector()
        self._bocpd_detector = BOCPDDetector()
        self._setpoint_scada_detector = SetpointAwareScadaDetector()
        self._band_breach_detector = BandBreachDetector()
        # Per-tenant cached diurnal profiles: {customer_id: {sensor: {hour: (mean, std)}}}
        self._diurnal_profiles_cache: dict[str, Dict[str, Dict[int, tuple]]] = {}
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

        attributor = AnomalyAttributor(self.detector.model)
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
    ) -> Dict[str, Any]:
        """Execute the full pipeline for one sequence.

        ``climate_context`` may carry climate-computer extras
        (``t_setpoint``, ``t_plant``, ``t_outdoor``, ``i_solar``, …).
        """
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
    ) -> "PipelineResult":
        """Core pipeline logic shared by ``run()`` and ``run_batch()``."""
        now = datetime.now(timezone.utc)
        seq_ts = now
        if sequence_timestamp:
            try:
                seq_ts = datetime.fromisoformat(sequence_timestamp)
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
                "Polling dropout: >{:.0%} of sequence steps have no sensor data".format(
                    DropoutDetector.PERSISTENCE_THRESHOLD
                )
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
        rule_votes: dict[str, list[str]] = {}

        # 0a. Noise / precision degradation check (SCADA spec 1.4).
        # Denormalize so per-sensor detection uses physical units (°C, %, ppm,
        # lux) consistent with SENSOR_NOISE_TARGETS in src/config.py.
        _denormed_for_noise = _denormalize_sequence(seq_array)

        # 0a+. Setpoint-aware climate / RH / microclimate / flatline rules.
        scada_anomalies: list[dict[str, Any]] = []
        _climate_ctx = climate_context_from_mapping(climate_context)
        _scada_findings = self._setpoint_scada_detector.detect(
            _denormed_for_noise,
            context=_climate_ctx,
            timestamp=seq_ts,
        )
        _SCADA_TYPE_TO_VOTE = {
            UNDER_HEATING: "under_heating",
            OVER_HEATING: "over_heating",
            DEHUMIDIFICATION_FAILURE: "dehumidification_failure",
            HUMIDITY_SATURATION_RISK: "humidity_saturation_risk",
            THERMAL_DIVERGENCE: "thermal_divergence",
            SENSOR_FLATLINE: "sensor_flatline",
        }
        for finding in _scada_findings:
            scada_anomalies.append(finding.to_dict())
            vote_key = _SCADA_TYPE_TO_VOTE.get(finding.anomaly_type)
            if vote_key:
                rule_votes.setdefault(vote_key, [])
                for sensor in finding.affected_sensors:
                    if sensor not in rule_votes[vote_key]:
                        rule_votes[vote_key].append(sensor)
                logger.warning(
                    "Setpoint SCADA %s for tenant %s: %s",
                    finding.anomaly_type,
                    customer_id,
                    finding.reasoning,
                )

        # 0a++. Operator crop band breaches (day/night min–max from OperationalConfig).
        # Band breaches are process/climate deviations, NOT sensor-fault evidence.
        # Collected in band_findings (separate from rule_votes) to preserve
        # fault-type precedence:
        #   (1) sensor-fault rule votes → (2) physics → (3) ensemble/LSTM → (4) band LAST
        # Sensor-trust gate and last-resort fusion happen after all sensor-fault
        # detectors have voted (step 5c-gate / 5c-last below).
        _op_config = get_operational_config_store().get(customer_id, greenhouse_id)
        _band_findings = self._band_breach_detector.detect(
            _denormed_for_noise,
            _op_config,
            timestamp=seq_ts,
        )
        band_findings: list[dict[str, Any]] = []
        for finding in _band_findings:
            finding_dict = finding.to_dict()
            scada_anomalies.append(finding_dict)
            band_findings.append(finding_dict)
            logger.warning(
                "Band breach %s for tenant %s: %s",
                finding.anomaly_type,
                customer_id,
                finding.reasoning,
            )

        # Seasonal σ scaler — factor 1.0 when baselines are sparse / absent.
        _season_key = _season_for_timestamp(seq_ts, _op_config.get("season_map") or {})
        _scaler = await self._get_seasonal_scaler(
            customer_id, greenhouse_id, _season_key, _op_config, db
        )

        noise_alarmed: list[str] = []
        for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            base_target = SENSOR_NOISE_TARGETS.get(s_name)
            target_override = (
                None
                if base_target is None
                else base_target * _scaler.factor(_season_key, s_name)
            )
            if self._noise_detector.detect(
                _denormed_for_noise[:, s_idx],
                s_name,
                target_override=target_override,
            ):
                noise_alarmed.append(s_name)
        if noise_alarmed:
            logger.warning(
                "Noise/precision degradation detected for tenant %s — sensors: %s",
                customer_id,
                noise_alarmed,
            )
            rule_votes["noise"] = noise_alarmed

        # 0b. Out-of-bounds check — short-circuit on hard physical limits.
        oob_alarmed: list[str] = []
        for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            if any(self._oob_detector.detect_series(_denormed_for_noise[:, s_idx], s_name)):
                oob_alarmed.append(s_name)
        if oob_alarmed:
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

        # 0c. Spike (MAD) check — short-circuit on transient outliers.
        # The per-sensor MAD floor keeps near-constant series (rolling MAD ~ 0)
        # from alarming on ordinary measurement noise.
        spike_alarmed: list[str] = []
        for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            base_floor = SPIKE_MAD_FLOOR.get(s_name, 0.0)
            spike_flags = self._spike_mad_detector.detect(
                _denormed_for_noise[:, s_idx],
                mad_floor=base_floor * _scaler.factor(_season_key, s_name),
            )
            if any(spike_flags):
                spike_alarmed.append(s_name)
        if spike_alarmed:
            logger.warning(
                "MAD spike detected for tenant %s — sensors: %s",
                customer_id,
                spike_alarmed,
            )
            rule_votes["spike_mad"] = spike_alarmed

        # 0d. Bias (EMA) check — detect sudden persistent offsets per sensor.
        # Thresholds are per sensor: the physical scales differ by orders of
        # magnitude, so a shared absolute limit over-flags lux/ppm channels.
        bias_ema_alarmed: list[str] = []
        for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            bias_flags_ema = BiasEMADetector.detect_ema(
                _denormed_for_noise[:, s_idx],
                threshold=BIAS_EMA_THRESHOLD_BY_SENSOR.get(s_name, BIAS_EMA_THRESHOLD),
            )
            if any(bias_flags_ema):
                bias_ema_alarmed.append(s_name)
        bias_ema_flag = len(bias_ema_alarmed) > 0
        if bias_ema_flag:
            logger.info(
                "Bias (EMA) detected for tenant %s — sensors: %s",
                customer_id,
                bias_ema_alarmed,
            )

        # 0e. Dual-sensor checks — bias cross-difference + correlation breakdown.
        if sensor_pairs:
            for pair in sensor_pairs:
                a = np.asarray(pair["sensor_a"], dtype=float)
                b = np.asarray(pair["sensor_b"], dtype=float)
                if len(a) != len(b) or len(a) < 2:
                    continue
                var_name = pair.get("variable", "unknown")

                bias_flags = self._bias_dual_detector.detect(a, b)
                if any(bias_flags):
                    logger.warning(
                        "Dual-sensor bias on %s for tenant %s",
                        var_name, customer_id,
                    )
                    rule_votes.setdefault("bias_dual", []).append(var_name)

                corr_flags = self._corr_breakdown_detector.detect(a, b)
                if any(corr_flags):
                    logger.warning(
                        "Correlation breakdown on %s for tenant %s",
                        var_name, customer_id,
                    )
                    rule_votes.setdefault("correlation_breakdown", []).append(var_name)

        # 0f. Diurnal profile check — hourly Z-score against historical stats.
        # Profiles auto-build from the tenant's stored history on first use.
        _diurnal_profiles = await self._ensure_diurnal_profiles(
            customer_id, greenhouse_id, db
        )
        if _diurnal_profiles:
            hour = seq_ts.hour
            diurnal_alarmed: list[str] = []
            for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
                profile = _diurnal_profiles.get(s_name)
                if profile and self._diurnal_detector.detect(
                    float(_denormed_for_noise[-1, s_idx]), hour, profile,
                ):
                    diurnal_alarmed.append(s_name)
            if diurnal_alarmed:
                logger.warning(
                    "Diurnal profile deviation for tenant %s — sensors: %s",
                    customer_id, diurnal_alarmed,
                )
                rule_votes["diurnal"] = diurnal_alarmed

        # 0g. Multivariate joint-implausibility (rolling Mahalanobis) — catches
        # in-range readings that are jointly impossible (plan P3). The last
        # timestep is the current reading under evaluation.
        mahal_flags = self._mahalanobis_detector.detect(_denormed_for_noise)
        if mahal_flags and mahal_flags[-1]:
            logger.warning(
                "Multivariate implausibility for tenant %s at current reading",
                customer_id,
            )
            rule_votes["multivariate"] = list(SENSOR_NAMES[: seq_array.shape[1]])

        # 0h. Change-point (BOCPD) — level shifts CUSUM's fixed K/H may miss.
        # A shift is only credible on multiple channels at once; a single
        # channel's step is usually a control action, not a fault.
        cp_sensors: list[str] = []
        for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            cp_flags = self._bocpd_detector.detect(_denormed_for_noise[:, s_idx])
            if cp_flags and cp_flags[-1]:
                cp_sensors.append(s_name)
        if len(cp_sensors) >= BOCPD_MIN_SENSORS:
            logger.warning(
                "Change-point detected for tenant %s — sensors: %s",
                customer_id, cp_sensors,
            )
            rule_votes["change_point"] = cp_sensors

        # 1. LSTM-VAE forward pass.
        _raw_score, anomaly_score, embedding, uncertainty = self._run_lstm_vae(seq_array)

        # 1a. CUSUM drift update — EMA-derived standardised residuals.
        # The EMABaselineEstimator tracks per-sensor running mean/variance and
        # produces standardised residuals that feed into the two-sided CUSUM.
        # Production-sensitive defaults: K=0.25, H=4.0 (detects 0.5σ shifts,
        # ARL₀ ≈ 340 samples).
        cusum_drift_alarm = False
        cusum_alarmed_sensors: list[str] = []
        _cusum = self._cusum_cache.setdefault(
            customer_id, CUSUMDriftDetector(k=CUSUM_K_SIGMA, h=CUSUM_H_SIGMA)
        )
        # Mutate H only (never rebuild) so LearningState round-trips stay valid.
        # Use mean factor across sensors as a single decision interval scale.
        _cusum_factors = [
            _scaler.factor(_season_key, s) for s in SENSOR_NAMES[: seq_array.shape[1]]
        ]
        _cusum.h = CUSUM_H_SIGMA * (
            sum(_cusum_factors) / len(_cusum_factors) if _cusum_factors else 1.0
        )
        _ema = self._ema_cache.setdefault(
            customer_id, EMABaselineEstimator()
        )
        _seq_last = seq_array[-1]  # most-recent timestep for residual computation
        for _idx, _sensor in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            _residual, _ = _ema.update(_sensor, float(_seq_last[_idx]))
            if _cusum.update(_sensor, _residual):
                cusum_drift_alarm = True
        if cusum_drift_alarm:
            cusum_alarmed_sensors = _cusum.alarmed_sensors()
            logger.info(
                "CUSUM drift alarm for tenant %s — sensors: %s",
                customer_id,
                cusum_alarmed_sensors,
            )

        # 2. Physics validation — may short-circuit the pipeline. Suppression
        # only quiets the model signal: a fired rule detector is direct
        # evidence of a sensor fault, so rule votes survive suppression
        # (pre-fusion, gates returned before physics ever ran).
        physics_result = self._run_physics_check(seq_array, now, climate_context)
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
        _effective_threshold = ANOMALY_THRESHOLD
        _ewma_tracker = _pot_fitter = None
        try:
            _ewma_tracker, _pot_fitter = await self._load_trackers(customer_id, db)
            _dynamic = max(_ewma_tracker.get_threshold(), _pot_fitter.get_threshold())
            if math.isfinite(_dynamic):
                _effective_threshold = _dynamic
        except Exception:
            logger.warning("EWMA/POT tracker load skipped (non-critical)", exc_info=True)

        # 5. Ensemble detection + fault classification.
        is_anomaly, fault_type, ensemble_result = self._run_ensemble_classify(
            seq_array=seq_array,
            baseline=baseline,
            anomaly_score=anomaly_score,
            effective_threshold=_effective_threshold,
            pattern_type=pattern_type,
            physics_result=physics_result,
        )

        # 5a. CUSUM drift: promotes is_anomaly and overrides fault_type.
        if cusum_drift_alarm and not physics_result.confirmed:
            is_anomaly = True
            fault_type = "calibration_drift"

        # 5b. Bias EMA: overrides fault_type when EMA-based bias was detected.
        if bias_ema_flag and is_anomaly and not physics_result.confirmed:
            fault_type = "bias_offset"

        # 5c. Rule-detector votes confirm the anomaly; the first fired rule
        # (gate order) takes fault-type precedence unless physics already
        # confirmed a specific fault.
        rule_profile: Optional[Dict[str, str]] = None
        if rule_votes:
            is_anomaly = True
            if not physics_result.confirmed:
                first_rule = next(iter(rule_votes))
                rule_profile = _RULE_VOTE_PROFILES[first_rule]
                fault_type = rule_profile["fault_type"]

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
        # Conditions (ALL must hold): physics did not suppress, no sensor-fault
        # rule fired, and the ensemble/LSTM/CUSUM statistical path did not flag.
        # BAND_TYPE_TO_VOTE provides the fault_type lookup; the profile is set
        # on rule_profile so the root-cause block (step 8) picks up the band's
        # severity/urgency and the reasoning is drawn from scada_anomalies.
        if (
            band_findings
            and not physics_result.suppressed
            and not rule_votes
            and not is_anomaly
        ):
            _first_band = band_findings[0]
            _band_vote_key = BAND_TYPE_TO_VOTE.get(
                str(_first_band.get("anomaly_type", ""))
            )
            _band_profile = _RULE_VOTE_PROFILES.get(_band_vote_key or "")
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

        # 6. Persist updated tracker scores.
        try:
            if _ewma_tracker is not None and _pot_fitter is not None:
                _ewma_tracker.update(anomaly_score)
                _pot_fitter.update(anomaly_score)
                await self._save_trackers(customer_id, _ewma_tracker, _pot_fitter, db)
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
                "reasoning": build_rule_reasoning(rule_votes),
            }
            # Prefer contextual setpoint-SCADA prose when those rules fired.
            if scada_anomalies:
                root_cause_result["reasoning"] = scada_anomalies[0]["reasoning"]
        recommendation_result = self.recommendation_engine.recommend(
            root_cause=root_cause_result["root_cause"],
            severity=root_cause_result.get("severity", "medium"),
        )
        reasoning = root_cause_result.get("reasoning", "")

        # Merge rule-vote evidence into the reported detector scores and
        # affected sensors (rule sensors first — they are direct evidence).
        detector_scores = dict(ensemble_result.detector_scores)
        affected_sensors = list(attribution_result["affected_sensors"])
        if rule_votes:
            for rule_name, rule_sensors in rule_votes.items():
                detector_scores[rule_name] = 1.0
                affected_sensors = [
                    *rule_sensors,
                    *(s for s in affected_sensors if s not in rule_sensors),
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
            fault_type="spike_fault",
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
        """Return a cached SeasonalScaler (factor 1.0 when baselines are sparse)."""
        cached = get_cached_seasonal_scaler(tenant_id, greenhouse_id, season)
        if cached is not None:
            return cached
        try:
            from src.api.services.seasonal_baseline_service import (
                compute_seasonal_baselines,
            )

            baselines = await compute_seasonal_baselines(
                db,
                tenant_id=tenant_id,
                greenhouse_id=greenhouse_id,
                season_map=op_config.get("season_map"),
            )
            scaler = build_scaler_from_baselines(
                baselines,
                overrides=op_config.get("seasonal_overrides"),
            )
        except Exception as exc:
            logger.warning(
                "Seasonal baseline compute failed for %s/%s: %s — using factor 1.0",
                tenant_id,
                greenhouse_id,
                exc,
            )
            scaler = SeasonalScaler()
        put_cached_seasonal_scaler(tenant_id, greenhouse_id, season, scaler)
        return scaler

    async def _ensure_diurnal_profiles(
        self,
        customer_id: str,
        greenhouse_id: str,
        db: AsyncSession,
    ) -> Dict[str, Dict[int, tuple]]:
        """Cached diurnal profiles, auto-built from stored history on miss.

        Format: ``{"temperature": {0: (mean, std), ...}, ...}``. An empty
        dict is cached for tenants without enough history so the build
        query runs at most once per tenant per process; profiles restored
        from LearningState by ``_load_trackers`` take the same cache slot.
        Failures never block the pipeline — the diurnal check is skipped.
        """
        cached = self._diurnal_profiles_cache.get(customer_id)
        if cached is not None:
            return cached

        from src.api.services.diurnal_profile_service import (  # noqa: PLC0415
            build_diurnal_profiles,
            persist_diurnal_profiles,
        )

        profiles: Dict[str, Dict[int, tuple]] = {}
        try:
            profiles = await build_diurnal_profiles(
                db, tenant_id=customer_id, greenhouse_id=greenhouse_id
            )
        except Exception:
            logger.warning(
                "Diurnal profile build failed for tenant %s (non-critical)",
                customer_id,
                exc_info=True,
            )
        self._diurnal_profiles_cache[customer_id] = profiles
        if profiles:
            logger.info(
                "Auto-built diurnal profiles for tenant %s (%d sensors)",
                customer_id,
                len(profiles),
            )
            try:
                await persist_diurnal_profiles(
                    db, tenant_id=customer_id, profiles=profiles
                )
            except Exception:
                logger.warning(
                    "Diurnal profile persist failed for tenant %s (non-critical)",
                    customer_id,
                    exc_info=True,
                )
        return profiles

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
        raw_score = float(raw_scores[0])
        anomaly_score = float(np.clip(raw_score / ANOMALY_SCALING_FACTOR, 0.0, 1.0))
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

        Returns:
            (is_anomaly, fault_type, ensemble_result)
        """
        ensemble = self._get_ensemble()
        ensemble_result = ensemble.detect(seq_array, baseline)
        is_lstm_anomaly = anomaly_score > effective_threshold
        physics_confirmed = physics_result.confirmed

        if physics_confirmed:
            is_anomaly = True
        else:
            is_anomaly = is_lstm_anomaly or ensemble_result.is_anomaly

        if physics_confirmed:
            fault_type: str = _PHYSICS_REASON_TO_FAULT.get(
                physics_result.reason, "spike_fault"
            )
        elif ensemble_result.fault_type is not None:
            # Stuck-sensor short-circuit from ensemble pre-check.
            fault_type = ensemble_result.fault_type
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

    async def _load_trackers(
        self,
        customer_id: str,
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
                    # Restore CUSUM + EMA state if persisted and not already in memory.
                    cusum_data = state.get("cusum_state")
                    if cusum_data is not None and customer_id not in self._cusum_cache:
                        self._cusum_cache[customer_id] = CUSUMDriftDetector.from_dict(
                            cusum_data
                        )
                    ema_data = state.get("ema_baseline_state")
                    if ema_data is not None and customer_id not in self._ema_cache:
                        self._ema_cache[customer_id] = EMABaselineEstimator.from_dict(
                            ema_data
                        )
                    diurnal_data = state.get("diurnal_profiles")
                    if diurnal_data is not None and customer_id not in self._diurnal_profiles_cache:
                        self._diurnal_profiles_cache[customer_id] = {
                            sensor: {int(h): tuple(v) for h, v in hours.items()}
                            for sensor, hours in diurnal_data.items()
                        }
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
        ewma: EWMATracker,
        pot: POTThresholdFitter,
        db: AsyncSession,
    ) -> None:
        """Persist EWMA, POT, and CUSUM trackers for a tenant into LearningState.state_json."""
        self._tracker_cache[customer_id] = (ewma, pot)

        tracker_payload = _json_safe({"ewma": ewma.to_dict(), "pot": pot.to_dict()})
        _cusum = self._cusum_cache.get(customer_id)
        _ema_bl = self._ema_cache.get(customer_id)
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

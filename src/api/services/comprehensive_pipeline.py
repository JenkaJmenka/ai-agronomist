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
    CUSUM_H_SIGMA,
    CUSUM_K_SIGMA,
    EWMA_ALPHA,
    EWMA_K,
    INPUT_DIM,
    NUMERICAL_EPSILON,
    POT_BUFFER_SIZE,
    POT_FPR,
    POT_Q,
    SEQ_LEN,
    SENSOR_NAMES,
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
from src.models.pattern_detector import PatternDetector
from src.models.physics_validator import PhysicsValidator
from src.models.pot_threshold import POTThresholdFitter
from src.models.recommendation_engine import RecommendationEngine
from src.models.retraining_orchestrator import RetrainingOrchestrator
from src.models.root_cause_analyzer import RootCauseAnalyzer
from src.monitoring.anomaly_metrics import AnomalyMetrics
from src.utils.lru import LRUDict

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "2.0"

# Map PhysicsValidator reason strings to fault type values.
_PHYSICS_REASON_TO_FAULT: Dict[str, str] = {
    "psychrometric_impossibility": "spike_fault",
    "nocturnal_light": "spike_fault",
    "co2_out_of_range": "calibration_drift",
    "temperature_out_of_range": "spike_fault",
}

# Sensor ranges in physical units for denormalization.
_DENORM_RANGES = np.array(
    [TEMP_RANGE, HUMIDITY_RANGE, CO2_RANGE, LIGHT_RANGE],
    dtype=np.float32,
)

# Minimum number of stored incidents before enabling dynamic thresholds.
_MIN_INCIDENTS_FOR_DYNAMIC_THRESHOLD: int = 50


def compute_hourly_stats(
    timestamps: List[datetime],
    values: List[float],
) -> Dict[int, tuple]:
    """Build per-hour (mean, std) from timestamped sensor readings.

    Args:
        timestamps: List of datetime objects.
        values: Corresponding sensor values in physical units.

    Returns:
        Dict mapping hour (0-23) to ``(mean, std)`` tuple.
        Hours with fewer than 5 samples are omitted.
    """
    from collections import defaultdict
    buckets: Dict[int, List[float]] = defaultdict(list)
    for ts, val in zip(timestamps, values):
        if val is not None and not (isinstance(val, float) and np.isnan(val)):
            buckets[ts.hour].append(val)

    result: Dict[int, tuple] = {}
    for hour, vals in buckets.items():
        if len(vals) >= 5:
            arr = np.array(vals)
            result[hour] = (float(np.mean(arr)), max(float(np.std(arr)), 1e-6))
    return result


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
    ) -> Dict[str, Any]:
        """Execute the full pipeline for one sequence."""
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
            return self._dropout_result(reason=_dropout_reason)

        # 0a. Noise / precision degradation check (SCADA spec 1.4).
        # Denormalize so per-sensor detection uses physical units (°C, %, ppm,
        # lux) consistent with SENSOR_NOISE_TARGETS in src/config.py.
        _denormed_for_noise = _denormalize_sequence(seq_array)
        noise_alarmed: list[str] = []
        for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            if self._noise_detector.detect(_denormed_for_noise[:, s_idx], s_name):
                noise_alarmed.append(s_name)
        if noise_alarmed:
            logger.warning(
                "Noise/precision degradation detected for tenant %s — sensors: %s",
                customer_id,
                noise_alarmed,
            )
            return self._build_noise_result(noise_alarmed)

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
            return self._build_oob_result(oob_alarmed)

        # 0c. Spike (MAD) check — short-circuit on transient outliers.
        spike_alarmed: list[str] = []
        for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            if any(self._spike_mad_detector.detect(_denormed_for_noise[:, s_idx])):
                spike_alarmed.append(s_name)
        if spike_alarmed:
            logger.warning(
                "MAD spike detected for tenant %s — sensors: %s",
                customer_id,
                spike_alarmed,
            )
            return self._build_spike_result(spike_alarmed)

        # 0d. Bias (EMA) check — detect sudden persistent offsets per sensor.
        bias_ema_alarmed: list[str] = []
        for s_idx, s_name in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            if any(BiasEMADetector.detect_ema(_denormed_for_noise[:, s_idx])):
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
                    return self._build_bias_dual_result(var_name)

                corr_flags = self._corr_breakdown_detector.detect(a, b)
                if any(corr_flags):
                    logger.warning(
                        "Correlation breakdown on %s for tenant %s",
                        var_name, customer_id,
                    )
                    return self._build_correlation_breakdown_result(var_name)

        # 0f. Diurnal profile check — hourly Z-score against historical stats.
        _diurnal_profiles = self._load_diurnal_profiles(customer_id)
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
                return self._build_diurnal_result(diurnal_alarmed, hour)

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

        # 2. Physics validation — may short-circuit the pipeline.
        physics_result = self._run_physics_check(seq_array, now)
        if physics_result.suppressed:
            logger.info(
                "Physically suppressed anomaly for tenant %s: %s",
                customer_id,
                physics_result.reason,
            )
            return self._suppressed_result(anomaly_score=anomaly_score, reason=physics_result.reason)
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

        # 8. Root cause + recommendations.
        root_cause_result = self.root_cause_analyzer.analyze(
            pattern_type=pattern_type,
            attribution=attribution_result["attribution"],
            sequence=seq_array,
        )
        recommendation_result = self.recommendation_engine.recommend(
            root_cause=root_cause_result["root_cause"],
            severity=root_cause_result.get("severity", "medium"),
        )
        root_cause_details = {
            "attribution": attribution_result["attribution"],
            "affected_sensors": attribution_result["affected_sensors"],
            "pattern_type": pattern_type,
            "pattern_explanation": pattern_result.get("explanation", ""),
            "sensor_values_at_peak": self._peak_sensor_values(seq_array),
            "detector_scores": ensemble_result.detector_scores,
            "ensemble_score": ensemble_result.ensemble_score,
            "ensemble_agreement": ensemble_result.agreement_count,
            "fault_type": fault_type,
            "physics_validation": physics_result.reason,
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
            affected_sensors=attribution_result["affected_sensors"],
            pattern_scores=pattern_result["pattern_scores"],
            root_cause=root_cause_result["root_cause"],
            root_cause_confidence=root_cause_result.get("confidence", 0.5),
            reasoning=root_cause_result.get("reasoning", ""),
            alternative_causes=root_cause_result.get("alternative_causes", []),
            severity=root_cause_result.get("severity", "medium"),
            urgency=root_cause_result.get("urgency", "normal"),
            immediate_actions=recommendation_result.get("immediate_actions", []),
            follow_up_actions=recommendation_result.get("follow_up_actions", []),
            all_recommendations=recommendation_result.get("all_actions", []),
            requires_technician=recommendation_result.get("requires_technician", False),
            estimated_resolution_minutes=recommendation_result.get("estimated_downtime_minutes", 30),
            incident_id=incident_id,
            detector_scores=ensemble_result.detector_scores,
            ensemble_score=ensemble_result.ensemble_score,
            ensemble_agreement=ensemble_result.agreement_count,
            physics_validation=physics_result.reason,
            learning_progress=learning_progress,
            timestamp=now.isoformat(),
            pipeline_version=PIPELINE_VERSION,
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
        )

    @staticmethod
    def _build_noise_result(alarmed_sensors: List[str]) -> "PipelineResult":
        """Result for a confirmed noise / precision degradation event.

        Returned before the LSTM-VAE forward pass.  anomaly_score is 1.0 as a
        maximum sentinel — the fault is confirmed by the noise detector, not by
        reconstruction error.
        """
        reason = (
            "Precision degradation detected on: "
            + ", ".join(alarmed_sensors)
        )
        return PipelineResult(
            is_anomaly=True,
            anomaly_score=1.0,
            confidence=1.0,
            confidence_level="very_high",
            pattern_type="accuracy_decline",
            fault_type="accuracy_decline",
            attribution={},
            affected_sensors=alarmed_sensors,
            pattern_scores={},
            root_cause="sensor_noise_floor_exceeded",
            root_cause_confidence=1.0,
            reasoning=reason,
            alternative_causes=[],
            severity="high",
            urgency="urgent",
            immediate_actions=["inspect_sensor_calibration", "check_sensor_hardware"],
            follow_up_actions=["replace_sensor_if_degraded", "recalibrate_sensor"],
            all_recommendations=[
                "inspect_sensor_calibration",
                "check_sensor_hardware",
                "replace_sensor_if_degraded",
                "recalibrate_sensor",
            ],
            requires_technician=True,
            estimated_resolution_minutes=60,
            incident_id=None,
            detector_scores={},
            ensemble_score=0.0,
            ensemble_agreement=0,
            physics_validation=None,
            learning_progress=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pipeline_version=PIPELINE_VERSION,
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
        )

    @staticmethod
    def _build_spike_result(alarmed_sensors: List[str]) -> "PipelineResult":
        """Result for a confirmed MAD spike anomaly."""
        reason = (
            "Transient spike (MAD) detected on: "
            + ", ".join(alarmed_sensors)
        )
        return PipelineResult(
            is_anomaly=True,
            anomaly_score=1.0,
            confidence=0.95,
            confidence_level="high",
            pattern_type="spike",
            fault_type="spike_fault",
            attribution={},
            affected_sensors=alarmed_sensors,
            pattern_scores={},
            root_cause="transient_spike",
            root_cause_confidence=0.9,
            reasoning=reason,
            alternative_causes=["electromagnetic_interference", "loose_connection"],
            severity="high",
            urgency="urgent",
            immediate_actions=["verify_reading", "check_sensor_connections"],
            follow_up_actions=["inspect_sensor_environment", "review_historical_data"],
            all_recommendations=[
                "verify_reading", "check_sensor_connections",
                "inspect_sensor_environment",
            ],
            requires_technician=False,
            estimated_resolution_minutes=15,
            incident_id=None,
            detector_scores={},
            ensemble_score=0.0,
            ensemble_agreement=0,
            physics_validation=None,
            learning_progress=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pipeline_version=PIPELINE_VERSION,
        )

    @staticmethod
    def _build_bias_dual_result(variable: str) -> "PipelineResult":
        """Result for a confirmed dual-sensor bias offset."""
        return PipelineResult(
            is_anomaly=True,
            anomaly_score=1.0,
            confidence=0.95,
            confidence_level="high",
            pattern_type="drift",
            fault_type="bias_offset",
            attribution={},
            affected_sensors=[variable],
            pattern_scores={},
            root_cause="redundant_sensor_bias_offset",
            root_cause_confidence=0.95,
            reasoning=f"Dual-sensor cross-difference exceeded threshold on {variable}",
            alternative_causes=["calibration_drift", "sensor_degradation"],
            severity="high",
            urgency="urgent",
            immediate_actions=["compare_sensor_readings", "recalibrate_sensors"],
            follow_up_actions=["replace_degraded_sensor"],
            all_recommendations=[
                "compare_sensor_readings", "recalibrate_sensors",
                "replace_degraded_sensor",
            ],
            requires_technician=True,
            estimated_resolution_minutes=45,
            incident_id=None,
            detector_scores={},
            ensemble_score=0.0,
            ensemble_agreement=0,
            physics_validation=None,
            learning_progress=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pipeline_version=PIPELINE_VERSION,
        )

    @staticmethod
    def _build_correlation_breakdown_result(variable: str) -> "PipelineResult":
        """Result for a confirmed spatial correlation breakdown."""
        return PipelineResult(
            is_anomaly=True,
            anomaly_score=1.0,
            confidence=0.90,
            confidence_level="high",
            pattern_type="dependency",
            fault_type="correlation_breakdown",
            attribution={},
            affected_sensors=[variable],
            pattern_scores={},
            root_cause="spatial_correlation_breakdown",
            root_cause_confidence=0.90,
            reasoning=f"Correlation between redundant sensors dropped below threshold on {variable}",
            alternative_causes=["sensor_failure", "environmental_anomaly"],
            severity="high",
            urgency="urgent",
            immediate_actions=["inspect_sensor_hardware", "verify_sensor_placement"],
            follow_up_actions=["replace_failed_sensor", "review_installation"],
            all_recommendations=[
                "inspect_sensor_hardware", "verify_sensor_placement",
                "replace_failed_sensor",
            ],
            requires_technician=True,
            estimated_resolution_minutes=60,
            incident_id=None,
            detector_scores={},
            ensemble_score=0.0,
            ensemble_agreement=0,
            physics_validation=None,
            learning_progress=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pipeline_version=PIPELINE_VERSION,
        )

    @staticmethod
    def _build_diurnal_result(alarmed_sensors: List[str], hour: int) -> "PipelineResult":
        """Result for a confirmed diurnal profile deviation."""
        reason = (
            f"Diurnal profile deviation at hour {hour} on: "
            + ", ".join(alarmed_sensors)
        )
        return PipelineResult(
            is_anomaly=True,
            anomaly_score=0.9,
            confidence=0.85,
            confidence_level="high",
            pattern_type="cyclic",
            fault_type="diurnal_deviation",
            attribution={},
            affected_sensors=alarmed_sensors,
            pattern_scores={},
            root_cause="diurnal_profile_violation",
            root_cause_confidence=0.85,
            reasoning=reason,
            alternative_causes=["environmental_change", "control_system_fault"],
            severity="medium",
            urgency="scheduled",
            immediate_actions=["verify_current_conditions", "check_control_setpoints"],
            follow_up_actions=["review_climate_history", "inspect_control_loop"],
            all_recommendations=[
                "verify_current_conditions", "check_control_setpoints",
                "review_climate_history",
            ],
            requires_technician=False,
            estimated_resolution_minutes=30,
            incident_id=None,
            detector_scores={},
            ensemble_score=0.0,
            ensemble_agreement=0,
            physics_validation=None,
            learning_progress=None,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pipeline_version=PIPELINE_VERSION,
        )

    def _load_diurnal_profiles(
        self, customer_id: str,
    ) -> Optional[Dict[str, Dict[int, tuple]]]:
        """Load cached diurnal profiles for a customer.

        Profiles are stored in LearningState.state_json under the key
        ``diurnal_profiles``.  Format::

            {"temperature": {0: [mean, std], 1: [mean, std], ...}, ...}

        Returns None if no profiles are available.
        """
        # Profiles are populated by compute_and_store_diurnal_profiles()
        # and cached in the diurnal_profiles_cache.
        return self._diurnal_profiles_cache.get(customer_id)

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
        self, seq_array: np.ndarray, now: datetime
    ) -> Any:
        """Denormalize sequence and run PhysicsValidator.validate()."""
        denormed = _denormalize_sequence(seq_array)
        return PhysicsValidator.validate(denormed, now)

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

        tracker_payload = {"ewma": ewma.to_dict(), "pot": pot.to_dict()}
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
                        initial_state["cusum_state"] = _cusum.to_dict()
                    if _ema_bl is not None:
                        initial_state["ema_baseline_state"] = _ema_bl.to_dict()
                    db.add(LearningState(
                        customer_id=customer_id,
                        state_json=initial_state,
                    ))
                else:
                    current = dict(row.state_json or {})
                    current["ewma_pot_trackers"] = tracker_payload
                    if _cusum is not None:
                        current["cusum_state"] = _cusum.to_dict()
                    if _ema_bl is not None:
                        current["ema_baseline_state"] = _ema_bl.to_dict()
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

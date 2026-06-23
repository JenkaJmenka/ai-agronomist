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
from src.models.confidence_scorer import ConfidenceScorer
from src.models.data_utils import SENSOR_RANGES
from src.models.cusum_drift_detector import CUSUMDriftDetector
from src.models.dropout_detector import DropoutDetector
from src.models.ensemble_detector import EnsembleAnomalyDetector, LstmVaeDetector
from src.models.noise_detector import NoiseDetector
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
        self._tracker_cache: dict[str, tuple[EWMATracker, POTThresholdFitter]] = {}
        # Polling dropout detector (shared across tenants — stateless).
        self._dropout_detector = DropoutDetector()
        # Noise / precision degradation detector (stateless, shared).
        self._noise_detector = NoiseDetector()
        # Per-tenant CUSUM drift detectors (stateful, persisted in LearningState).
        self._cusum_cache: dict[str, CUSUMDriftDetector] = {}

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
        return build_default_baseline()

    async def run(
        self,
        customer_id: str,
        sequence: List[List[float]],
        db: AsyncSession,
        greenhouse_id: str = "default",
        baseline_sequences: Optional[List[List[List[float]]]] = None,
    ) -> Dict[str, Any]:
        """Execute the full pipeline for one sequence."""
        seq_array = np.asarray(sequence, dtype=np.float32)
        if seq_array.shape != (SEQ_LEN, INPUT_DIM):
            raise ValueError(
                f"sequence shape must be ({SEQ_LEN}, {INPUT_DIM}), "
                f"got {seq_array.shape}"
            )

        baseline = self._prepare_baseline(baseline_sequences)

        attributor = AnomalyAttributor(self.detector.model)
        attributor.set_baseline(baseline)

        pattern_detector = PatternDetector()
        pattern_detector.set_baseline(baseline)

        return await self._run_with_components(
            customer_id=customer_id,
            seq_array=seq_array,
            sequence_raw=sequence,
            db=db,
            greenhouse_id=greenhouse_id,
            baseline=baseline,
            attributor=attributor,
            pattern_detector=pattern_detector,
        )

    async def run_batch(
        self,
        customer_id: str,
        sequences: List[np.ndarray],
        db: AsyncSession,
        greenhouse_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Execute the full pipeline for multiple sequences.

        Creates one ``AnomalyAttributor`` and one ``PatternDetector``,
        calls ``set_baseline`` once, then reuses them across all sequences
        in the batch.
        """
        baseline = self._prepare_baseline(None)

        attributor = AnomalyAttributor(self.detector.model)
        attributor.set_baseline(baseline)

        pattern_detector = PatternDetector()
        pattern_detector.set_baseline(baseline)

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
    ) -> "PipelineResult":
        """Core pipeline logic shared by ``run()`` and ``run_batch()``."""
        now = datetime.now(timezone.utc)

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

        # 1. LSTM-VAE forward pass.
        _raw_score, anomaly_score, embedding, uncertainty = self._run_lstm_vae(seq_array)

        # 1a. CUSUM drift update — per-sensor standardised residuals.
        # Baseline statistics are derived from the provided (or synthetic)
        # baseline sequences so every tenant uses its own reference distribution.
        cusum_drift_alarm = False
        cusum_alarmed_sensors: list[str] = []
        _baseline_mean = baseline.mean(axis=(0, 1))            # shape (INPUT_DIM,)
        _baseline_std = baseline.std(axis=(0, 1)).clip(        # shape (INPUT_DIM,)
            min=NUMERICAL_EPSILON
        )
        _cusum = self._cusum_cache.setdefault(
            customer_id, CUSUMDriftDetector(k=CUSUM_K_SIGMA, h=CUSUM_H_SIGMA)
        )
        _seq_last = seq_array[-1]  # most-recent timestep for residual computation
        # Minimum meaningful baseline std in normalised [0,1] units.
        # When baseline is near-constant (e.g. synthetic midpoint baseline),
        # standardised residuals blow up and CUSUM alarms spuriously.
        _CUSUM_MIN_BASELINE_STD: float = 1e-3
        for _idx, _sensor in enumerate(SENSOR_NAMES[: seq_array.shape[1]]):
            _std = float(_baseline_std[_idx])
            if _std >= _CUSUM_MIN_BASELINE_STD:
                _residual = (
                    float(_seq_last[_idx]) - float(_baseline_mean[_idx])
                ) / _std
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

        # 5a. CUSUM override: a confirmed drift alarm promotes fault_type to
        # calibration_drift when the sequence is already flagged as anomalous.
        # Physics-confirmed fault types take precedence over CUSUM.
        if cusum_drift_alarm and is_anomaly and not physics_result.confirmed:
            fault_type = "calibration_drift"

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
                    # Restore CUSUM state if persisted and not already in memory.
                    cusum_data = state.get("cusum_state")
                    if cusum_data is not None and customer_id not in self._cusum_cache:
                        self._cusum_cache[customer_id] = CUSUMDriftDetector.from_dict(
                            cusum_data
                        )
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
                    db.add(LearningState(
                        customer_id=customer_id,
                        state_json=initial_state,
                    ))
                else:
                    current = dict(row.state_json or {})
                    current["ewma_pot_trackers"] = tracker_payload
                    if _cusum is not None:
                        current["cusum_state"] = _cusum.to_dict()
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

"""
Multi-detector ensemble for greenhouse anomaly scoring (Gap 4).

Combines LSTM-VAE reconstruction error, Isolation Forest, Mahalanobis
statistical distance, and correlation/range violations into a weighted
vote with agreement counting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, Sequence

import numpy as np
from sklearn.ensemble import IsolationForest

from src.config import (
    ENSEMBLE_ANOMALY_THRESHOLD,
    ENSEMBLE_CORRELATION_THRESHOLD,
    ENSEMBLE_DETECTOR_WEIGHTS,
    ENSEMBLE_ISOLATION_FOREST_THRESHOLD,
    ENSEMBLE_LSTM_VAE_THRESHOLD,
    ENSEMBLE_MIN_AGREEMENT,
    ENSEMBLE_STATISTICAL_THRESHOLD,
    SENSOR_NAMES,
    TEMP_RANGE,
    HUMIDITY_RANGE,
    CO2_RANGE,
    LIGHT_RANGE,
)
from src.models.bias_detector import BiasOffsetDetector
from src.models.inference import AnomalyDetector
from src.models.stuck_fault_detector import StuckFaultDetector

logger = logging.getLogger(__name__)

DETECTOR_NAMES = ("lstm_vae", "isolation_forest", "statistical", "correlation")

_SENSOR_BOUNDS = {
    "temperature": TEMP_RANGE,
    "humidity": HUMIDITY_RANGE,
    "co2": CO2_RANGE,
    "light": LIGHT_RANGE,
}


class DetectorProtocol(Protocol):
    """Minimal interface for ensemble member detectors."""

    name: str
    threshold: float

    def score(self, sequence: np.ndarray, baseline: np.ndarray) -> float:
        """Return anomaly score in [0, 1] (higher = more anomalous)."""

    def is_anomaly(self, score: float) -> bool:
        """Return whether score exceeds detector threshold."""


@dataclass(frozen=True)
class EnsembleResult:
    """Aggregated ensemble output."""

    detector_scores: Dict[str, float]
    ensemble_score: float
    agreement_count: int
    is_anomaly: bool
    bias_flag: bool = False
    fault_type: Optional[str] = None
    stuck_sensors: Optional[Dict[str, bool]] = None


def _clip_unit(value: float) -> float:
    # Non-finite inputs (e.g. NaN from a zero-variance correlation matrix) must
    # collapse to 0.0, not propagate: a NaN score poisons the ensemble vote and
    # cannot be serialized into a Postgres JSON column (asyncpg rejects "NaN").
    if not np.isfinite(value):
        return 0.0
    return float(np.clip(value, 0.0, 1.0))


def _normalize_error(error: float, threshold: float) -> float:
    if threshold <= 0:
        return 1.0 if error > 0 else 0.0
    return _clip_unit(error / threshold)


class LstmVaeDetector:
    """Wrap :class:`AnomalyDetector` reconstruction error as ensemble member."""

    name = "lstm_vae"

    def __init__(
        self,
        anomaly_detector: AnomalyDetector,
        threshold: float = ENSEMBLE_LSTM_VAE_THRESHOLD,
    ) -> None:
        self._detector = anomaly_detector
        self.threshold = threshold

    def score(self, sequence: np.ndarray, baseline: np.ndarray) -> float:
        del baseline
        batch = sequence[np.newaxis, ...] if sequence.ndim == 2 else sequence
        errors = self._detector.predict(batch)
        peak_error = float(np.max(errors))
        return _normalize_error(peak_error, self.threshold)

    def is_anomaly(self, score: float) -> bool:
        return score >= 0.5


class IsolationForestDetector:
    """sklearn Isolation Forest on flattened sequences."""

    name = "isolation_forest"

    def __init__(
        self,
        threshold: float = ENSEMBLE_ISOLATION_FOREST_THRESHOLD,
        random_state: int = 42,
    ) -> None:
        self.threshold = threshold
        self._random_state = random_state
        self._model: Optional[IsolationForest] = None
        self._score_min = 0.0
        self._score_max = 1.0

    def fit(self, baseline: np.ndarray) -> None:
        """Fit on baseline sequences ``(n, seq_len, input_dim)``."""
        flat = baseline.reshape(len(baseline), -1)
        self._model = IsolationForest(
            contamination=0.05,
            random_state=self._random_state,
            n_estimators=50,
        )
        self._model.fit(flat)
        raw = -self._model.score_samples(flat)
        self._score_min = float(np.min(raw))
        self._score_max = float(np.max(raw))
        if self._score_max <= self._score_min:
            self._score_max = self._score_min + 1e-6

    def score(self, sequence: np.ndarray, baseline: np.ndarray) -> float:
        if self._model is None:
            self.fit(baseline)
        if self._model is None:
            raise RuntimeError("IsolationForestDetector not fitted — call fit() first")
        flat = sequence.reshape(1, -1)
        raw = float(-self._model.score_samples(flat)[0])
        normalized = (raw - self._score_min) / (self._score_max - self._score_min)
        return _clip_unit(normalized)

    def is_anomaly(self, score: float) -> bool:
        return score >= self.threshold


class StatisticalDetector:
    """Mahalanobis distance of sequence mean vs baseline distribution."""

    name = "statistical"

    def __init__(self, threshold: float = ENSEMBLE_STATISTICAL_THRESHOLD) -> None:
        self.threshold = threshold
        self._mean: Optional[np.ndarray] = None
        self._inv_cov: Optional[np.ndarray] = None

    def fit(self, baseline: np.ndarray) -> None:
        """Compute mean and inverse covariance from baseline timestep means."""
        per_seq_means = baseline.mean(axis=1)
        if len(per_seq_means) < 2:
            self._mean = per_seq_means[0]
            self._inv_cov = np.eye(len(self._mean))
            return
        self._mean = per_seq_means.mean(axis=0)
        cov = np.cov(per_seq_means.T)
        if cov.ndim == 0:
            cov = np.array([[float(cov)]])
        cov += np.eye(cov.shape[0]) * 1e-6
        self._inv_cov = np.linalg.inv(cov)

    def score(self, sequence: np.ndarray, baseline: np.ndarray) -> float:
        if self._mean is None or self._inv_cov is None:
            self.fit(baseline)
        if self._mean is None or self._inv_cov is None:
            raise RuntimeError("StatisticalDetector not fitted — call fit() first")
        seq_mean = sequence.mean(axis=0)
        diff = seq_mean - self._mean
        mahal = float(np.sqrt(diff @ self._inv_cov @ diff))
        return _normalize_error(mahal, self.threshold)

    def is_anomaly(self, score: float) -> bool:
        return score >= 0.5


class CorrelationDetector:
    """Range violations and cross-sensor correlation breaks vs baseline."""

    name = "correlation"

    def __init__(self, threshold: float = ENSEMBLE_CORRELATION_THRESHOLD) -> None:
        self.threshold = threshold
        self._baseline_corr: Optional[np.ndarray] = None

    def fit(self, baseline: np.ndarray) -> None:
        flat = baseline.reshape(-1, baseline.shape[-1])
        self._baseline_corr = np.corrcoef(flat.T)

    def score(self, sequence: np.ndarray, baseline: np.ndarray) -> float:
        if self._baseline_corr is None:
            self.fit(baseline)
        if self._baseline_corr is None:
            raise RuntimeError("CorrelationDetector not fitted — call fit() first")

        ncols = sequence.shape[1]
        sensor_subset = SENSOR_NAMES[:ncols]
        mins = np.array([_SENSOR_BOUNDS[s][0] for s in sensor_subset])
        maxs = np.array([_SENSOR_BOUNDS[s][1] for s in sensor_subset])
        range_violations = int(np.sum((sequence < mins) | (sequence > maxs)))
        total_checks = sequence.size

        range_score = range_violations / max(total_checks, 1)

        flat = sequence.reshape(-1, sequence.shape[-1])
        if flat.shape[0] < 2:
            corr_score = 0.0
        else:
            seq_corr = np.corrcoef(flat.T)
            diff = np.abs(seq_corr - self._baseline_corr)
            corr_score = float(np.nanmean(diff))

        combined = 0.6 * range_score + 0.4 * _clip_unit(corr_score)
        return _clip_unit(combined)

    def is_anomaly(self, score: float) -> bool:
        return score >= self.threshold


class EnsembleAnomalyDetector:
    """Weighted vote across four detectors with agreement counting."""

    def __init__(
        self,
        lstm_detector: LstmVaeDetector,
        weights: Optional[Dict[str, float]] = None,
        anomaly_threshold: float = ENSEMBLE_ANOMALY_THRESHOLD,
        min_agreement: int = ENSEMBLE_MIN_AGREEMENT,
    ) -> None:
        self._lstm = lstm_detector
        self._iforest = IsolationForestDetector()
        self._statistical = StatisticalDetector()
        self._correlation = CorrelationDetector()
        self._detectors: Sequence[DetectorProtocol] = (
            self._lstm,
            self._iforest,
            self._statistical,
            self._correlation,
        )
        self._weights = dict(weights or ENSEMBLE_DETECTOR_WEIGHTS)
        self._anomaly_threshold = anomaly_threshold
        self._min_agreement = min_agreement
        self._baseline_cache: Optional[np.ndarray] = None
        self._stuck_detector = StuckFaultDetector()
        self._bias_detector = BiasOffsetDetector(
            expected_ranges={
                "temperature": TEMP_RANGE,
                "humidity": HUMIDITY_RANGE,
                "co2": CO2_RANGE,
                "light": LIGHT_RANGE,
            }
        )

    def fit_baseline(self, baseline: np.ndarray) -> None:
        """Pre-fit sklearn/stat detectors on customer baseline."""
        self._baseline_cache = baseline
        self._iforest.fit(baseline)
        self._statistical.fit(baseline)
        self._correlation.fit(baseline)

    def detect(
        self,
        sequence: np.ndarray,
        baseline: np.ndarray,
    ) -> EnsembleResult:
        """Score sequence and return ensemble result.

        Runs stuck-fault and bias-offset pre-checks before the 4-detector
        weighted vote.  A stuck sensor short-circuits to ``is_anomaly=True``
        with ``fault_type="stuck_fault"``.  Bias detection sets a flag
        propagated through the result for downstream classification.
        """
        # --- Pre-check: stuck fault ---
        stuck_result = self._stuck_detector.detect(sequence)
        has_stuck = any(stuck_result.values())

        if has_stuck:
            return EnsembleResult(
                detector_scores={name: 0.0 for name in DETECTOR_NAMES},
                ensemble_score=1.0,
                agreement_count=len(DETECTOR_NAMES),
                is_anomaly=True,
                fault_type="stuck_fault",
                bias_flag=False,
                stuck_sensors=stuck_result,
            )

        # --- Pre-check: bias offset ---
        bias_result = self._bias_detector.detect(sequence)
        bias_flag = any(mag > 0.0 for mag in bias_result.values())

        # --- Standard 4-detector vote ---
        if self._baseline_cache is None or baseline is not self._baseline_cache:
            self.fit_baseline(baseline)

        scores: Dict[str, float] = {}
        votes = 0
        weighted_sum = 0.0
        weight_total = 0.0

        for detector in self._detectors:
            raw = detector.score(sequence, baseline)
            scores[detector.name] = round(raw, 4)
            weight = self._weights.get(detector.name, 0.25)
            weighted_sum += weight * raw
            weight_total += weight
            if detector.is_anomaly(raw):
                votes += 1

        ensemble_score = weighted_sum / weight_total if weight_total > 0 else 0.0
        is_anomaly = (
            ensemble_score >= self._anomaly_threshold
            and votes >= self._min_agreement
        )

        return EnsembleResult(
            detector_scores=scores,
            ensemble_score=round(ensemble_score, 4),
            agreement_count=votes,
            is_anomaly=is_anomaly,
            bias_flag=bias_flag,
        )

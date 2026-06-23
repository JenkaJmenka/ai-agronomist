"""
Confidence scoring: HOW CONFIDENT are we that an anomaly is real (not noise)?

Combines four signals into one confidence value in [0, 1]:

1. RECONSTRUCTION ERROR strength — error relative to the normal-data baseline
   (``CONFIDENCE_ERROR_RATIO_SCALE`` x baseline maps to a full signal).
2. PATTERN CLARITY — gap between the top-2 pattern scores from
   :class:`src.models.pattern_detector.PatternDetector` (one clear pattern is
   more trustworthy than several confused candidates).
3. ENSEMBLE AGREEMENT — fraction of detectors that flagged the anomaly.
4. UNCERTAINTY (inverse) — embedding uncertainty from the LSTM-VAE latent
   space; low uncertainty means high confidence.

All weights and thresholds live in :mod:`src.config` (``CONFIDENCE_*``).

Example output of :meth:`ConfidenceScorer.assess`::

    {
        "confidence": 0.92,
        "is_high_confidence": True,
        "is_low_confidence": False,
        "confidence_level": "very_high",
        "signals": {"recon_error": 1.0, "pattern_clarity": 0.9, ...}
    }
"""

import logging
import math
from typing import Any, Dict, Mapping, Optional

import numpy as np

from src.config import (
    CONFIDENCE_DEFAULT_BASELINE_ERROR,
    CONFIDENCE_ENSEMBLE_SIZE,
    CONFIDENCE_ERROR_RATIO_SCALE,
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_LEVEL_BOUNDS,
    CONFIDENCE_LOW_THRESHOLD,
    CONFIDENCE_MIN_LEVEL,
    CONFIDENCE_SIGNAL_WEIGHTS,
    NUMERICAL_EPSILON,
    PATTERN_CONFIDENCE_GAP_SCALE,
)

logger = logging.getLogger(__name__)

# Tolerance when validating that the signal weights sum to 1.0.
_WEIGHT_SUM_TOLERANCE: float = 1e-6


class ConfidenceScorer:
    """Scores confidence that a detected anomaly is real rather than noise.

    Usage:
        scorer = ConfidenceScorer()
        confidence = scorer.score(
            reconstruction_error=0.20,
            pattern_scores=detection["pattern_scores"],
            ensemble_agreement=3,
            uncertainty=0.08,
        )
        verdict = scorer.assess(...)  # full dict contract

    Pure numpy/python — no torch model involved. Consumes the Phase 1.2
    ``PatternDetector.detect()['pattern_scores']`` contract and the Gap 1
    embedding uncertainty (mean latent std) convention.
    """

    SIGNAL_NAMES: tuple = tuple(CONFIDENCE_SIGNAL_WEIGHTS)

    def __init__(self, weights: Optional[Mapping[str, float]] = None) -> None:
        """
        Args:
            weights: Optional mapping with exactly the keys ``recon_error``,
                ``pattern_clarity``, ``ensemble_agreement`` and
                ``uncertainty``; values must be finite, non-negative and sum
                to 1.0. Defaults to ``config.CONFIDENCE_SIGNAL_WEIGHTS``.

        Raises:
            ValueError: If keys are missing/unknown, any weight is negative
                or non-finite, or the weights do not sum to 1.0.
        """
        self.weights: Dict[str, float] = dict(
            weights if weights is not None else CONFIDENCE_SIGNAL_WEIGHTS
        )
        self._validate_weights()
        _bound_values = [b for b, _ in CONFIDENCE_LEVEL_BOUNDS]
        if _bound_values != sorted(_bound_values, reverse=True):
            raise RuntimeError(
                "CONFIDENCE_LEVEL_BOUNDS must be sorted in descending order; "
                "check src/config.py"
            )

    def score(
        self,
        reconstruction_error: float,
        pattern_scores: Mapping[str, float],
        ensemble_agreement: int,
        uncertainty: float,
        baseline_error: float = CONFIDENCE_DEFAULT_BASELINE_ERROR,
    ) -> float:
        """Compute the combined anomaly confidence in [0, 1].

        Confidence is high when the reconstruction error well exceeds the
        baseline, one pattern clearly dominates, most ensemble detectors
        agree, and the embedding uncertainty is low.

        Args:
            reconstruction_error: Non-negative reconstruction error of the
                anomalous sequence.
            pattern_scores: Per-pattern scores in [0, 1], e.g.
                ``{"drift": 0.1, "spike": 0.8, ...}`` from
                ``PatternDetector.detect()``.
            ensemble_agreement: Number of ensemble detectors that flagged
                the anomaly, in [0, ``CONFIDENCE_ENSEMBLE_SIZE``].
            uncertainty: Non-negative embedding uncertainty (mean latent
                std); values >= 1 are treated as maximal.
            baseline_error: Positive expected reconstruction error on
                normal data.

        Returns:
            Weighted confidence, clipped to [0, 1].

        Raises:
            ValueError: On non-finite, negative or out-of-range inputs, or
                an empty/invalid ``pattern_scores`` mapping.
        """
        signals = self.compute_signals(
            reconstruction_error=reconstruction_error,
            pattern_scores=pattern_scores,
            ensemble_agreement=ensemble_agreement,
            uncertainty=uncertainty,
            baseline_error=baseline_error,
        )
        confidence = sum(
            self.weights[signal_name] * signal_value
            for signal_name, signal_value in signals.items()
        )
        return float(np.clip(confidence, 0.0, 1.0))

    def compute_signals(
        self,
        reconstruction_error: float,
        pattern_scores: Mapping[str, float],
        ensemble_agreement: int,
        uncertainty: float,
        baseline_error: float = CONFIDENCE_DEFAULT_BASELINE_ERROR,
    ) -> Dict[str, float]:
        """Compute the four individual confidence signals, each in [0, 1].

        Args/Raises: see :meth:`score` (same contract; this validates).

        Returns:
            ``{"recon_error", "pattern_clarity", "ensemble_agreement",
            "uncertainty"}`` — each signal already normalized to [0, 1],
            BEFORE weighting.
        """
        self._validate_inputs(
            reconstruction_error, ensemble_agreement, uncertainty, baseline_error
        )
        scores = self._validate_pattern_scores(pattern_scores)
        return {
            "recon_error": self._score_reconstruction_error(
                reconstruction_error, baseline_error
            ),
            "pattern_clarity": self._score_pattern_clarity(scores),
            "ensemble_agreement": ensemble_agreement / CONFIDENCE_ENSEMBLE_SIZE,
            "uncertainty": 1.0 - min(uncertainty, 1.0),
        }

    def assess(
        self,
        reconstruction_error: float,
        pattern_scores: Mapping[str, float],
        ensemble_agreement: int,
        uncertainty: float,
        baseline_error: float = CONFIDENCE_DEFAULT_BASELINE_ERROR,
    ) -> Dict[str, Any]:
        """Full assessment contract for downstream consumers.

        Args/Raises: see :meth:`score`.

        Returns:
            Dict with ``confidence`` (float in [0, 1]),
            ``is_high_confidence``, ``is_low_confidence`` (bool),
            ``confidence_level`` (str) and ``signals`` (the unweighted
            per-signal breakdown from :meth:`compute_signals`).
        """
        signals = self.compute_signals(
            reconstruction_error=reconstruction_error,
            pattern_scores=pattern_scores,
            ensemble_agreement=ensemble_agreement,
            uncertainty=uncertainty,
            baseline_error=baseline_error,
        )
        confidence = float(
            np.clip(
                sum(self.weights[name] * value for name, value in signals.items()),
                0.0,
                1.0,
            )
        )
        return {
            "confidence": confidence,
            "is_high_confidence": self.high_confidence(confidence),
            "is_low_confidence": self.low_confidence(confidence),
            "confidence_level": self.confidence_level(confidence),
            "signals": signals,
        }

    def high_confidence(
        self, confidence: float, threshold: float = CONFIDENCE_HIGH_THRESHOLD
    ) -> bool:
        """True when ``confidence >= threshold`` (actionable anomaly)."""
        self._validate_confidence(confidence)
        return confidence >= threshold

    def low_confidence(
        self, confidence: float, threshold: float = CONFIDENCE_LOW_THRESHOLD
    ) -> bool:
        """True when ``confidence <= threshold`` (might be noise)."""
        self._validate_confidence(confidence)
        return confidence <= threshold

    def confidence_level(self, confidence: float) -> str:
        """Human-readable level per ``CONFIDENCE_LEVEL_BOUNDS``.

        Returns:
            One of ``very_high``/``high``/``medium``/``low``/``very_low``.

        Raises:
            ValueError: If ``confidence`` is non-finite or outside [0, 1].
        """
        self._validate_confidence(confidence)
        for lower_bound, label in CONFIDENCE_LEVEL_BOUNDS:
            if confidence >= lower_bound:
                return label
        return CONFIDENCE_MIN_LEVEL

    def _score_reconstruction_error(
        self, reconstruction_error: float, baseline_error: float
    ) -> float:
        """Error relative to baseline; SCALE x baseline maps to 1.0."""
        error_ratio = reconstruction_error / (baseline_error + NUMERICAL_EPSILON)
        return min(error_ratio / CONFIDENCE_ERROR_RATIO_SCALE, 1.0)

    def _score_pattern_clarity(self, scores: np.ndarray) -> float:
        """Gap between the top-2 pattern scores, scaled to [0, 1].

        Mirrors ``PatternDetector._compute_confidence`` (same
        ``PATTERN_CONFIDENCE_GAP_SCALE``); a single-entry mapping treats the
        runner-up as 0, so one lone strong pattern is maximally clear.
        """
        top_score = float(scores.max())
        second_score = (
            float(np.partition(scores, -2)[-2]) if scores.size > 1 else 0.0
        )
        gap = top_score - second_score
        return min(gap * PATTERN_CONFIDENCE_GAP_SCALE, 1.0)

    def _validate_weights(self) -> None:
        """Raise ValueError unless weights are complete, finite, sum to 1."""
        expected_keys = set(CONFIDENCE_SIGNAL_WEIGHTS)
        actual_keys = set(self.weights)
        if actual_keys != expected_keys:
            raise ValueError(
                f"Weights must have exactly the keys {sorted(expected_keys)}, "
                f"got {sorted(actual_keys)}"
            )
        for signal_name, weight in self.weights.items():
            if not math.isfinite(weight) or weight < 0.0:
                raise ValueError(
                    f"Weight '{signal_name}' must be finite and >= 0, "
                    f"got {weight}"
                )
        total_weight = sum(self.weights.values())
        if abs(total_weight - 1.0) >= _WEIGHT_SUM_TOLERANCE:
            raise ValueError(f"Weights must sum to 1.0, got {total_weight}")

    def _validate_inputs(
        self,
        reconstruction_error: float,
        ensemble_agreement: int,
        uncertainty: float,
        baseline_error: float,
    ) -> None:
        """Reject non-finite / out-of-range scalar inputs at the boundary."""
        if not math.isfinite(reconstruction_error) or reconstruction_error < 0.0:
            raise ValueError(
                f"reconstruction_error must be finite and >= 0, "
                f"got {reconstruction_error}"
            )
        if not math.isfinite(baseline_error) or baseline_error <= 0.0:
            raise ValueError(
                f"baseline_error must be finite and > 0, got {baseline_error}"
            )
        if not math.isfinite(uncertainty) or uncertainty < 0.0:
            raise ValueError(
                f"uncertainty must be finite and >= 0, got {uncertainty}"
            )
        if (
            not float(ensemble_agreement).is_integer()
            or not 0 <= ensemble_agreement <= CONFIDENCE_ENSEMBLE_SIZE
        ):
            raise ValueError(
                f"ensemble_agreement must be an integer in "
                f"[0, {CONFIDENCE_ENSEMBLE_SIZE}], got {ensemble_agreement}"
            )

    def _validate_pattern_scores(
        self, pattern_scores: Mapping[str, float]
    ) -> np.ndarray:
        """Validate the pattern-scores mapping; return its values as floats.

        Raises:
            ValueError: If the mapping is empty or any score is non-finite
                or outside [0, 1].
        """
        if not pattern_scores:
            raise ValueError("pattern_scores must be a non-empty mapping")
        scores = np.asarray(list(pattern_scores.values()), dtype=np.float64)
        if not np.isfinite(scores).all():
            raise ValueError(
                f"pattern_scores contains non-finite values: {dict(pattern_scores)}"
            )
        if (scores < 0.0).any() or (scores > 1.0).any():
            raise ValueError(
                f"pattern_scores values must be in [0, 1], "
                f"got {dict(pattern_scores)}"
            )
        return scores

    def _validate_confidence(self, confidence: float) -> None:
        """Reject non-finite or out-of-range confidence values."""
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError(
                f"confidence must be finite and in [0, 1], got {confidence}"
            )

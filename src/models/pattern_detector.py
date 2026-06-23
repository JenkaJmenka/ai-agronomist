"""
Pattern detection: identifies WHAT TYPE of anomaly occurred.

7 pattern types:
1. DRIFT: sensor stuck, in-sequence variance near 0
2. SPIKE: sudden short deviation (z-score above threshold)
3. DEPENDENCY: expected pairwise correlation breaks
4. LAG: normally correlated sensors now correlated at a delay
5. SATURATION: sensor railed at/beyond its baseline min/max
6. CYCLIC: normal periodicity (e.g. diurnal light cycle) disappears
7. CASCADE: multiple sensors deviate strongly at the same time

The per-pattern scoring lives in :mod:`src.models.pattern_scores` (pure,
config-tuned functions); this module validates inputs, orchestrates the
scorers, picks the primary pattern and renders sensor-aware explanations.

Example output of :meth:`PatternDetector.detect`::

    {
        "primary_pattern": "dependency",
        "primary_score": 0.85,
        "pattern_scores": {"drift": 0.1, "spike": 0.2, ...},
        "affected_sensors": ["temperature", "co2"],
        "confidence": 0.92,
        "explanation": "Expected correlation broken (temperature, co2)"
    }
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from src.config import (
    PATTERN_CONFIDENCE_GAP_SCALE,
    PATTERN_MIN_SEQ_LEN,
    SENSOR_NAMES,
)
from src.models.pattern_scores import (
    PatternBaseline,
    compute_baseline,
    score_cascade,
    score_cyclic,
    score_dependency,
    score_drift,
    score_lag,
    score_saturation,
    score_spike,
)

logger = logging.getLogger(__name__)

# Scorer signature: (sequence, baseline) -> (score, affected sensor indices).
PatternScorer = Callable[
    [np.ndarray, PatternBaseline], Tuple[float, np.ndarray]
]

# Insertion order doubles as the tie-break priority for the primary pattern.
_PATTERN_SCORERS: Dict[str, PatternScorer] = {
    "drift": score_drift,
    "spike": score_spike,
    "dependency": score_dependency,
    "lag": score_lag,
    "saturation": score_saturation,
    "cyclic": score_cyclic,
    "cascade": score_cascade,
}

_EXPLANATION_TEMPLATES: Dict[str, str] = {
    "drift": "Sensor(s) {sensors} stuck at constant value",
    "spike": "Sudden deviation in {sensors}",
    "dependency": "Expected correlation broken ({sensors})",
    "lag": "Delayed response in {sensors}",
    "saturation": "Sensor(s) {sensors} at boundary",
    "cyclic": "Daily pattern disrupted in {sensors}",
    "cascade": "Multi-sensor cascade detected ({sensors})",
}

_NO_SENSORS_ISOLATED: str = "no specific sensors isolated"


class PatternDetector:
    """Detects which TYPE of anomaly a sensor sequence exhibits.

    Usage:
        detector = PatternDetector()
        detector.set_baseline(normal_sequences)  # (N, seq_len, input_dim)
        result = detector.detect(sequence)       # (seq_len, input_dim)

    All detection thresholds live in :mod:`src.config` (``PATTERN_*``) so
    each detector can be tuned without touching code.
    """

    PATTERN_TYPES: Tuple[str, ...] = tuple(_PATTERN_SCORERS)

    def __init__(self, sensor_names: Optional[List[str]] = None) -> None:
        """
        Args:
            sensor_names: Per-channel sensor names; defaults to
                ``config.SENSOR_NAMES``. Length defines the expected
                ``input_dim`` of every sequence.

        Raises:
            ValueError: If ``sensor_names`` is provided but empty.
        """
        self.sensor_names: List[str] = (
            list(sensor_names) if sensor_names is not None else list(SENSOR_NAMES)
        )
        if not self.sensor_names:
            raise ValueError("sensor_names must contain at least one sensor")
        self.baseline: Optional[PatternBaseline] = None

    def set_baseline(self, sequences: np.ndarray) -> None:
        """Establish the normal-operation baseline used by all scorers.

        Computes per-sensor mean/std/min/max, pairwise correlations and
        dominant-frequency spectra.

        Args:
            sequences: Array of shape ``(N, seq_len, input_dim)`` of normal
                (non-anomalous) sequences.

        Raises:
            ValueError: If the array is not 3-D, is empty, is too short along
                the time axis, has the wrong sensor count, or contains
                non-finite values (NaN/Inf).
        """
        baseline_sequences = self._validate_baseline(sequences)
        self.baseline = compute_baseline(baseline_sequences)
        logger.info(
            "Pattern baseline set from %d sequences (seq_len=%d, sensors=%s)",
            self.baseline.num_sequences,
            self.baseline.sequence_length,
            self.sensor_names,
        )

    def detect(self, sequence: np.ndarray) -> Dict[str, Any]:
        """Score all seven pattern types for one sequence.

        Args:
            sequence: Array of shape ``(seq_len, input_dim)``.

        Returns:
            Dict with keys ``primary_pattern`` (highest-scoring type;
            ties resolve in ``PATTERN_TYPES`` order), ``primary_score``,
            ``pattern_scores`` (all 7, each in [0, 1]), ``affected_sensors``
            (names exhibiting the primary pattern), ``confidence``
            (gap between the top-2 scores, scaled to [0, 1]) and
            ``explanation`` (sensor-aware natural language).

        Raises:
            ValueError: If no baseline is set, the shape is invalid, the
                sequence is too short, or it contains non-finite values.
        """
        sequence = self._validate_sequence(sequence)
        assert self.baseline is not None  # narrowed by validation

        pattern_scores: Dict[str, float] = {}
        affected_by_pattern: Dict[str, np.ndarray] = {}
        for pattern_type, scorer in _PATTERN_SCORERS.items():
            score, affected_indices = scorer(sequence, self.baseline)
            pattern_scores[pattern_type] = score
            affected_by_pattern[pattern_type] = affected_indices

        primary_pattern = max(pattern_scores, key=pattern_scores.__getitem__)
        affected_sensors = [
            self.sensor_names[sensor_idx]
            for sensor_idx in affected_by_pattern[primary_pattern]
        ]
        return {
            "primary_pattern": primary_pattern,
            "primary_score": pattern_scores[primary_pattern],
            "pattern_scores": pattern_scores,
            "affected_sensors": affected_sensors,
            "confidence": self._compute_confidence(pattern_scores),
            "explanation": self._explain_pattern(primary_pattern, affected_sensors),
        }

    def detect_batch(self, sequences: np.ndarray) -> List[Dict[str, Any]]:
        """Detect patterns for a batch of sequences.

        Args:
            sequences: Array of shape ``(batch, seq_len, input_dim)``.

        Returns:
            One detection dict (see :meth:`detect`) per sequence.
        """
        return [self.detect(sequence) for sequence in sequences]

    def _compute_confidence(self, pattern_scores: Dict[str, float]) -> float:
        """Confidence = how clearly the primary pattern stands out.

        The gap between the top-2 scores is scaled by
        ``PATTERN_CONFIDENCE_GAP_SCALE`` and clipped to [0, 1]; a tie at the
        top yields 0.0.
        """
        scores = np.array(list(pattern_scores.values()), dtype=float)
        top_score = scores.max()
        second_score = np.partition(scores, -2)[-2]
        gap = top_score - second_score
        return float(min(gap * PATTERN_CONFIDENCE_GAP_SCALE, 1.0))

    def _explain_pattern(
        self, pattern_type: str, affected_sensors: List[str]
    ) -> str:
        """Render a sensor-aware natural-language explanation."""
        sensors_text = ", ".join(affected_sensors) or _NO_SENSORS_ISOLATED
        template = _EXPLANATION_TEMPLATES.get(pattern_type)
        if template is None:
            return "Unknown pattern"
        return template.format(sensors=sensors_text)

    def _validate_baseline(self, sequences: np.ndarray) -> np.ndarray:
        """Validate and coerce baseline sequences to a float64 3-D array.

        Raises:
            ValueError: On wrong rank, empty batch, short time axis, sensor
                count mismatch, or non-finite values.
        """
        baseline_sequences = np.asarray(sequences, dtype=np.float64)
        if baseline_sequences.ndim != 3 or baseline_sequences.shape[0] == 0:
            raise ValueError(
                f"Baseline must be a non-empty (N, seq_len, input_dim) array, "
                f"got shape {baseline_sequences.shape}"
            )
        if baseline_sequences.shape[1] < PATTERN_MIN_SEQ_LEN:
            raise ValueError(
                f"Baseline sequences need at least {PATTERN_MIN_SEQ_LEN} time "
                f"steps, got {baseline_sequences.shape[1]}"
            )
        if baseline_sequences.shape[2] != len(self.sensor_names):
            raise ValueError(
                f"Baseline has {baseline_sequences.shape[2]} sensors, expected "
                f"{len(self.sensor_names)} ({self.sensor_names})"
            )
        if not np.isfinite(baseline_sequences).all():
            raise ValueError(
                "Baseline contains non-finite values (NaN/Inf); "
                "clean the sensor data before setting the baseline"
            )
        return baseline_sequences

    def _validate_sequence(self, sequence: np.ndarray) -> np.ndarray:
        """Validate and coerce one sequence to a float64 2-D array.

        Raises:
            ValueError: If no baseline is set, the shape is invalid, the
                sequence is too short, or it contains non-finite values.
        """
        if self.baseline is None:
            raise ValueError("Must call set_baseline() first")
        sequence = np.asarray(sequence, dtype=np.float64)
        if sequence.ndim != 2 or sequence.shape[1] != len(self.sensor_names):
            raise ValueError(
                f"Sequence must have shape (seq_len, {len(self.sensor_names)}), "
                f"got {sequence.shape}"
            )
        if sequence.shape[0] < PATTERN_MIN_SEQ_LEN:
            raise ValueError(
                f"Sequence needs at least {PATTERN_MIN_SEQ_LEN} time steps, "
                f"got {sequence.shape[0]}"
            )
        if not np.isfinite(sequence).all():
            raise ValueError(
                "Sequence contains non-finite values (NaN/Inf); "
                "clean the sensor data before pattern detection"
            )
        return sequence

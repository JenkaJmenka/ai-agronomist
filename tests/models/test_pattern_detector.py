"""Smoke tests for the Phase 1.2 Pattern Detector (7 anomaly pattern types).

All randomness is seeded for reproducibility. Fixtures build three kinds of
baselines: plain noise around a constant level (spec smoke tests), a
correlated sensor pair (dependency/lag tests) and a periodic light channel
(cyclic tests).
"""

import numpy as np
import pytest

from src.config import (
    PATTERN_CONFIDENCE_GAP_SCALE,
    PATTERN_MIN_SEQ_LEN,
    PATTERN_SPIKE_MAX_DURATION_FRACTION,
    SENSOR_NAMES,
    SEQ_LEN,
)
from src.models.pattern_detector import PatternDetector
from src.models.pattern_scores import (
    compute_baseline,
    score_cascade,
    score_cyclic,
    score_dependency,
    score_drift,
    score_lag,
    score_saturation,
    score_spike,
)

NUM_SENSORS = len(SENSOR_NAMES)
BASELINE_LEVEL = 22.0
BASELINE_NOISE_STD = 0.1
NUM_BASELINE_SEQUENCES = 100
SINE_PERIOD = 10  # time steps per cycle (2 full cycles in SEQ_LEN=20)


def _sine_wave(seq_len: int = SEQ_LEN, shift: int = 0) -> np.ndarray:
    """Unit sine with SINE_PERIOD, optionally delayed by `shift` steps."""
    time_steps = np.arange(seq_len)
    return np.sin(2.0 * np.pi * (time_steps - shift) / SINE_PERIOD)


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(42)


@pytest.fixture
def detector(rng: np.random.Generator) -> PatternDetector:
    """Detector with a plain noise baseline around 22.0 (spec fixture)."""
    pattern_detector = PatternDetector()
    baseline = rng.normal(
        BASELINE_LEVEL, BASELINE_NOISE_STD, size=(NUM_BASELINE_SEQUENCES, SEQ_LEN, NUM_SENSORS)
    )
    pattern_detector.set_baseline(baseline)
    return pattern_detector


def _correlated_baseline(rng: np.random.Generator) -> np.ndarray:
    """Baseline where sensors 0 and 1 share a sine signal (corr ~1)."""
    baseline = rng.normal(
        BASELINE_LEVEL, 1.0, size=(NUM_BASELINE_SEQUENCES, SEQ_LEN, NUM_SENSORS)
    )
    shared_signal = _sine_wave()
    baseline[:, :, 0] = shared_signal + rng.normal(
        0.0, 0.05, size=(NUM_BASELINE_SEQUENCES, SEQ_LEN)
    )
    baseline[:, :, 1] = shared_signal + rng.normal(
        0.0, 0.05, size=(NUM_BASELINE_SEQUENCES, SEQ_LEN)
    )
    return baseline


@pytest.fixture
def correlated_detector(rng: np.random.Generator) -> PatternDetector:
    pattern_detector = PatternDetector()
    pattern_detector.set_baseline(_correlated_baseline(rng))
    return pattern_detector


def _cyclic_baseline(rng: np.random.Generator) -> np.ndarray:
    """Baseline where the light channel (index 3) is strongly periodic."""
    baseline = rng.normal(
        BASELINE_LEVEL, 1.0, size=(NUM_BASELINE_SEQUENCES, SEQ_LEN, NUM_SENSORS)
    )
    baseline[:, :, 3] = 500.0 + 400.0 * _sine_wave() + rng.normal(
        0.0, 10.0, size=(NUM_BASELINE_SEQUENCES, SEQ_LEN)
    )
    return baseline


@pytest.fixture
def cyclic_detector(rng: np.random.Generator) -> PatternDetector:
    pattern_detector = PatternDetector()
    pattern_detector.set_baseline(_cyclic_baseline(rng))
    return pattern_detector


def _normal_sequence(rng: np.random.Generator) -> np.ndarray:
    return rng.normal(BASELINE_LEVEL, BASELINE_NOISE_STD, size=(SEQ_LEN, NUM_SENSORS))


# =============================================================================
# Spec smoke tests
# =============================================================================


class TestSpecSmoke:
    def test_drift_detection(self, detector: PatternDetector) -> None:
        """Should detect drift (stuck sensor) on an all-constant sequence."""
        sequence = np.ones((SEQ_LEN, NUM_SENSORS)) * BASELINE_LEVEL
        result = detector.detect(sequence)
        assert result["pattern_scores"]["drift"] > 0.5

    def test_spike_detection(self, detector: PatternDetector, rng) -> None:
        """Should detect a single-point spike."""
        sequence = _normal_sequence(rng)
        sequence[10, 0] = 50.0
        result = detector.detect(sequence)
        assert result["pattern_scores"]["spike"] > 0.3

    def test_saturation_detection(self, detector: PatternDetector) -> None:
        """Should detect saturation when pinned beyond the baseline range."""
        sequence = np.full((SEQ_LEN, NUM_SENSORS), 100.0)
        result = detector.detect(sequence)
        assert result["pattern_scores"]["saturation"] > 0.5

    def test_primary_pattern_identified(self, detector: PatternDetector) -> None:
        """Should identify a primary pattern from the known set."""
        sequence = np.ones((SEQ_LEN, NUM_SENSORS)) * BASELINE_LEVEL
        result = detector.detect(sequence)
        assert result["primary_pattern"] in detector.PATTERN_TYPES

    def test_confidence_in_range(self, detector: PatternDetector, rng) -> None:
        """Confidence should be within [0, 1]."""
        result = detector.detect(_normal_sequence(rng))
        assert 0.0 <= result["confidence"] <= 1.0

    def test_affected_sensors_identified(self, detector: PatternDetector, rng) -> None:
        """Should identify which sensors are affected."""
        sequence = _normal_sequence(rng)
        sequence[:, 0] = 50.0
        result = detector.detect(sequence)
        assert len(result["affected_sensors"]) > 0

    def test_explanation_generated(self, detector: PatternDetector) -> None:
        """Should generate a non-trivial explanation string."""
        sequence = np.ones((SEQ_LEN, NUM_SENSORS)) * BASELINE_LEVEL
        result = detector.detect(sequence)
        assert isinstance(result["explanation"], str)
        assert len(result["explanation"]) > 5

    def test_all_patterns_covered(self, detector: PatternDetector, rng) -> None:
        """All 7 pattern types should be scored."""
        result = detector.detect(_normal_sequence(rng))
        assert len(result["pattern_scores"]) == 7
        assert set(result["pattern_scores"]) == set(detector.PATTERN_TYPES)

    def test_pattern_scores_in_range(self, detector: PatternDetector, rng) -> None:
        """Every pattern score should be within [0, 1]."""
        result = detector.detect(_normal_sequence(rng))
        assert all(0.0 <= score <= 1.0 for score in result["pattern_scores"].values())


# =============================================================================
# DRIFT
# =============================================================================


class TestDrift:
    def test_constant_sequence_scores_full_drift(self, detector) -> None:
        sequence = np.ones((SEQ_LEN, NUM_SENSORS)) * BASELINE_LEVEL
        score, affected = score_drift(sequence, detector.baseline)
        assert score == 1.0
        assert affected.tolist() == list(range(NUM_SENSORS))

    def test_normal_sequence_scores_no_drift(self, detector, rng) -> None:
        score, affected = score_drift(_normal_sequence(rng), detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_single_stuck_sensor_isolated(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[:, 2] = BASELINE_LEVEL
        score, affected = score_drift(sequence, detector.baseline)
        assert score == pytest.approx(1.0 / NUM_SENSORS)
        assert affected.tolist() == [2]

    def test_drift_is_primary_for_stuck_sequence(self, detector) -> None:
        sequence = np.ones((SEQ_LEN, NUM_SENSORS)) * BASELINE_LEVEL
        result = detector.detect(sequence)
        assert result["primary_pattern"] == "drift"


# =============================================================================
# SPIKE
# =============================================================================


class TestSpike:
    def test_spike_affected_sensor_isolated(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[5, 1] = 80.0
        score, affected = score_spike(sequence, detector.baseline)
        assert score > 0.3
        assert 1 in affected.tolist()

    def test_sustained_shift_is_not_a_spike(self, detector) -> None:
        """A whole-sequence excursion is a shift, not a sudden spike."""
        sequence = np.full((SEQ_LEN, NUM_SENSORS), 100.0)
        score, affected = score_spike(sequence, detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_normal_sequence_scores_low_spike(self, detector, rng) -> None:
        score, _ = score_spike(_normal_sequence(rng), detector.baseline)
        assert score < 0.5

    def test_huge_spike_saturates_score_at_one(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[10, 0] = 1000.0
        score, _ = score_spike(sequence, detector.baseline)
        assert score == 1.0

    def test_spike_is_primary_for_spiked_sequence(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[10, 0] = 50.0
        result = detector.detect(sequence)
        assert result["primary_pattern"] == "spike"
        assert SENSOR_NAMES[0] in result["affected_sensors"]


# =============================================================================
# DEPENDENCY
# =============================================================================


class TestDependency:
    def test_inverted_correlation_detected(self, correlated_detector, rng) -> None:
        sequence = rng.normal(BASELINE_LEVEL, 1.0, size=(SEQ_LEN, NUM_SENSORS))
        sequence[:, 0] = _sine_wave()
        sequence[:, 1] = -_sine_wave()  # baseline corr ~ +1, now ~ -1
        score, affected = score_dependency(sequence, correlated_detector.baseline)
        assert score > 0.3
        assert {0, 1}.issubset(set(affected.tolist()))

    def test_intact_correlation_scores_lower(self, correlated_detector, rng) -> None:
        intact = rng.normal(BASELINE_LEVEL, 1.0, size=(SEQ_LEN, NUM_SENSORS))
        intact[:, 0] = _sine_wave() + rng.normal(0.0, 0.05, SEQ_LEN)
        intact[:, 1] = _sine_wave() + rng.normal(0.0, 0.05, SEQ_LEN)
        intact_score, _ = score_dependency(intact, correlated_detector.baseline)

        broken = intact.copy()
        broken[:, 1] = -broken[:, 1]
        broken_score, _ = score_dependency(broken, correlated_detector.baseline)
        assert broken_score > intact_score

    def test_constant_sequence_yields_finite_score(self, detector) -> None:
        """Constant channels make corrcoef undefined — must not produce NaN."""
        sequence = np.full((SEQ_LEN, NUM_SENSORS), BASELINE_LEVEL)
        score, _ = score_dependency(sequence, detector.baseline)
        assert np.isfinite(score)
        assert 0.0 <= score <= 1.0


# =============================================================================
# LAG
# =============================================================================


class TestLag:
    def _lagged_sequence(self, rng, shift: int) -> np.ndarray:
        sequence = rng.normal(BASELINE_LEVEL, 1.0, size=(SEQ_LEN, NUM_SENSORS))
        sequence[:, 0] = _sine_wave()
        sequence[:, 1] = _sine_wave(shift=shift)
        return sequence

    def test_shifted_pair_detected(self, correlated_detector, rng) -> None:
        sequence = self._lagged_sequence(rng, shift=3)
        score, affected = score_lag(sequence, correlated_detector.baseline)
        assert score == 1.0  # only candidate pair (0, 1) is lagged
        assert affected.tolist() == [0, 1]

    def test_aligned_pair_not_lagged(self, correlated_detector, rng) -> None:
        sequence = self._lagged_sequence(rng, shift=0)
        sequence[:, 0] += rng.normal(0.0, 0.05, SEQ_LEN)
        sequence[:, 1] += rng.normal(0.0, 0.05, SEQ_LEN)
        score, affected = score_lag(sequence, correlated_detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_short_sequence_returns_zero(self, correlated_detector, rng) -> None:
        sequence = rng.normal(BASELINE_LEVEL, 1.0, size=(5, NUM_SENSORS))
        score, affected = score_lag(sequence, correlated_detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_no_correlated_baseline_pairs_returns_zero(self, detector, rng) -> None:
        """Plain-noise baseline has no candidate pairs -> lag is 0."""
        score, affected = score_lag(_normal_sequence(rng), detector.baseline)
        assert score == 0.0
        assert affected.size == 0


# =============================================================================
# SATURATION
# =============================================================================


class TestSaturation:
    def test_pinned_above_baseline_max(self, detector) -> None:
        sequence = np.full((SEQ_LEN, NUM_SENSORS), 100.0)
        score, affected = score_saturation(sequence, detector.baseline)
        assert score == 1.0
        assert affected.tolist() == list(range(NUM_SENSORS))

    def test_pinned_below_baseline_min(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[:, 1] = -50.0
        score, affected = score_saturation(sequence, detector.baseline)
        assert score == pytest.approx(1.0 / NUM_SENSORS)
        assert affected.tolist() == [1]

    def test_constant_mid_range_is_not_saturation(self, detector) -> None:
        """Stuck at the baseline mean is drift, NOT boundary saturation."""
        sequence = np.full((SEQ_LEN, NUM_SENSORS), BASELINE_LEVEL)
        score, affected = score_saturation(sequence, detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_normal_sequence_scores_no_saturation(self, detector, rng) -> None:
        score, _ = score_saturation(_normal_sequence(rng), detector.baseline)
        assert score == 0.0


# =============================================================================
# CYCLIC
# =============================================================================


class TestCyclic:
    def test_broken_cycle_detected(self, cyclic_detector, rng) -> None:
        sequence = rng.normal(BASELINE_LEVEL, 1.0, size=(SEQ_LEN, NUM_SENSORS))
        sequence[:, 3] = 500.0 + rng.normal(0.0, 10.0, SEQ_LEN)  # flat light
        score, affected = score_cyclic(sequence, cyclic_detector.baseline)
        assert score == 1.0  # only cyclic sensor (light) is broken
        assert affected.tolist() == [3]

    def test_intact_cycle_not_flagged(self, cyclic_detector, rng) -> None:
        sequence = rng.normal(BASELINE_LEVEL, 1.0, size=(SEQ_LEN, NUM_SENSORS))
        sequence[:, 3] = 500.0 + 400.0 * _sine_wave() + rng.normal(0.0, 10.0, SEQ_LEN)
        score, affected = score_cyclic(sequence, cyclic_detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_non_cyclic_baseline_returns_zero(self, detector, rng) -> None:
        score, affected = score_cyclic(_normal_sequence(rng), detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_mismatched_length_returns_zero(self, cyclic_detector, rng) -> None:
        """FFT bins are incomparable across different sequence lengths."""
        sequence = rng.normal(BASELINE_LEVEL, 1.0, size=(SEQ_LEN // 2, NUM_SENSORS))
        score, affected = score_cyclic(sequence, cyclic_detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_baseline_spectra_identify_light_period(self, rng) -> None:
        baseline = compute_baseline(_cyclic_baseline(rng))
        # Period 10 over 20 samples = rfft bin 2 = non-DC index 1.
        assert baseline.dominant_frequency_bins[3] == 1
        assert baseline.dominant_power_fractions[3] > 0.3


# =============================================================================
# CASCADE
# =============================================================================


class TestCascade:
    def test_coordinated_shift_detected(self, detector, rng) -> None:
        sequence = _normal_sequence(rng) + 10.0  # all sensors deviate together
        score, affected = score_cascade(sequence, detector.baseline)
        assert score == 1.0
        assert affected.tolist() == list(range(NUM_SENSORS))

    def test_single_sensor_deviation_is_not_cascade(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[:, 0] += 10.0
        score, affected = score_cascade(sequence, detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_normal_sequence_scores_no_cascade(self, detector, rng) -> None:
        score, _ = score_cascade(_normal_sequence(rng), detector.baseline)
        assert score == 0.0

    def test_two_sensor_cascade_partial_score(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[:, 0] += 10.0
        sequence[:, 2] += 10.0
        score, affected = score_cascade(sequence, detector.baseline)
        assert score == pytest.approx(2.0 / NUM_SENSORS)
        assert affected.tolist() == [0, 2]


# =============================================================================
# Input validation (boundaries)
# =============================================================================


class TestValidation:
    def test_detect_without_baseline_raises(self) -> None:
        with pytest.raises(ValueError, match="set_baseline"):
            PatternDetector().detect(np.zeros((SEQ_LEN, NUM_SENSORS)))

    def test_detect_rejects_nan(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[3, 2] = np.nan
        with pytest.raises(ValueError, match="non-finite"):
            detector.detect(sequence)

    def test_detect_rejects_inf(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[0, 0] = np.inf
        with pytest.raises(ValueError, match="non-finite"):
            detector.detect(sequence)

    def test_detect_rejects_wrong_rank(self, detector) -> None:
        with pytest.raises(ValueError, match="shape"):
            detector.detect(np.zeros(SEQ_LEN))

    def test_detect_rejects_wrong_sensor_count(self, detector) -> None:
        with pytest.raises(ValueError, match="shape"):
            detector.detect(np.zeros((SEQ_LEN, NUM_SENSORS + 1)))

    def test_detect_rejects_too_short_sequence(self, detector) -> None:
        with pytest.raises(ValueError, match="time steps"):
            detector.detect(np.zeros((1, NUM_SENSORS)))

    def test_set_baseline_rejects_nan(self) -> None:
        baseline = np.zeros((5, SEQ_LEN, NUM_SENSORS))
        baseline[0, 0, 0] = np.nan
        with pytest.raises(ValueError, match="non-finite"):
            PatternDetector().set_baseline(baseline)

    def test_set_baseline_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            PatternDetector().set_baseline(np.zeros((0, SEQ_LEN, NUM_SENSORS)))

    def test_set_baseline_rejects_wrong_rank(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            PatternDetector().set_baseline(np.zeros((SEQ_LEN, NUM_SENSORS)))

    def test_set_baseline_rejects_sensor_mismatch(self) -> None:
        with pytest.raises(ValueError, match="sensors"):
            PatternDetector().set_baseline(np.zeros((5, SEQ_LEN, NUM_SENSORS + 1)))

    def test_empty_sensor_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            PatternDetector(sensor_names=[])


# =============================================================================
# Result contract & helpers
# =============================================================================


class TestResultContract:
    EXPECTED_KEYS = {
        "primary_pattern",
        "primary_score",
        "pattern_scores",
        "affected_sensors",
        "confidence",
        "explanation",
    }

    def test_result_has_all_keys(self, detector, rng) -> None:
        result = detector.detect(_normal_sequence(rng))
        assert set(result) == self.EXPECTED_KEYS

    def test_default_sensor_names_from_config(self) -> None:
        assert PatternDetector().sensor_names == list(SENSOR_NAMES)

    def test_custom_sensor_names_used(self, rng) -> None:
        names = ["a", "b"]
        pattern_detector = PatternDetector(sensor_names=names)
        baseline = rng.normal(0.0, 1.0, size=(20, SEQ_LEN, 2))
        pattern_detector.set_baseline(baseline)
        sequence = np.zeros((SEQ_LEN, 2))
        result = pattern_detector.detect(sequence)
        assert set(result["affected_sensors"]).issubset(set(names))

    def test_affected_sensors_subset_of_names(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        sequence[:, 0] = 100.0
        result = detector.detect(sequence)
        assert set(result["affected_sensors"]).issubset(set(SENSOR_NAMES))

    def test_detect_accepts_list_input(self, detector) -> None:
        sequence = [[BASELINE_LEVEL] * NUM_SENSORS for _ in range(SEQ_LEN)]
        result = detector.detect(sequence)
        assert result["primary_pattern"] == "drift"

    def test_detect_is_deterministic(self, detector, rng) -> None:
        sequence = _normal_sequence(rng)
        first = detector.detect(sequence)
        second = detector.detect(sequence)
        assert first == second

    def test_detect_batch_returns_one_result_per_sequence(self, detector, rng) -> None:
        sequences = rng.normal(
            BASELINE_LEVEL, BASELINE_NOISE_STD, size=(3, SEQ_LEN, NUM_SENSORS)
        )
        results = detector.detect_batch(sequences)
        assert len(results) == 3
        assert all(set(item) == self.EXPECTED_KEYS for item in results)

    def test_confidence_full_for_clear_winner(self, detector) -> None:
        scores = {"a": 1.0, "b": 0.0, "c": 0.0}
        assert detector._compute_confidence(scores) == 1.0

    def test_confidence_zero_for_tied_top_scores(self, detector) -> None:
        scores = {"a": 0.8, "b": 0.8, "c": 0.1}
        assert detector._compute_confidence(scores) == 0.0

    def test_explanation_mentions_affected_sensors(self, detector) -> None:
        explanation = detector._explain_pattern("drift", ["temperature", "co2"])
        assert "temperature" in explanation
        assert "co2" in explanation

    def test_explanation_for_every_pattern_type(self, detector) -> None:
        for pattern_type in detector.PATTERN_TYPES:
            explanation = detector._explain_pattern(pattern_type, ["temperature"])
            assert "temperature" in explanation
            assert len(explanation) > 5

    def test_explanation_handles_no_affected_sensors(self, detector) -> None:
        explanation = detector._explain_pattern("dependency", [])
        assert "no specific sensors isolated" in explanation

    def test_unknown_pattern_explanation(self, detector) -> None:
        assert detector._explain_pattern("nonsense", []) == "Unknown pattern"


# =============================================================================
# Baseline statistics
# =============================================================================


class TestBaselineStats:
    def test_baseline_mean_and_std(self, rng) -> None:
        sequences = rng.normal(
            BASELINE_LEVEL, BASELINE_NOISE_STD, size=(NUM_BASELINE_SEQUENCES, SEQ_LEN, NUM_SENSORS)
        )
        baseline = compute_baseline(sequences)
        assert baseline.mean == pytest.approx(
            [BASELINE_LEVEL] * NUM_SENSORS, abs=0.05
        )
        assert baseline.std == pytest.approx(
            [BASELINE_NOISE_STD] * NUM_SENSORS, abs=0.05
        )

    def test_baseline_min_max_bound_data(self, rng) -> None:
        sequences = rng.normal(0.0, 1.0, size=(10, SEQ_LEN, NUM_SENSORS))
        baseline = compute_baseline(sequences)
        flattened = sequences.reshape(-1, NUM_SENSORS)
        assert np.allclose(baseline.min_values, flattened.min(axis=0))
        assert np.allclose(baseline.max_values, flattened.max(axis=0))

    def test_baseline_correlations_capture_shared_signal(self, rng) -> None:
        baseline = compute_baseline(_correlated_baseline(rng))
        assert baseline.correlations[0, 1] > 0.9
        assert abs(baseline.correlations[2, 3]) < 0.3

    def test_baseline_metadata(self, rng) -> None:
        sequences = rng.normal(0.0, 1.0, size=(7, SEQ_LEN, NUM_SENSORS))
        baseline = compute_baseline(sequences)
        assert baseline.num_sequences == 7
        assert baseline.sequence_length == SEQ_LEN


# =============================================================================
# QA extension — validation edges (Inf, short baseline, list input, reset)
# =============================================================================


class TestQaValidationEdges:
    def test_set_baseline_rejects_inf(self) -> None:
        baseline = np.zeros((5, SEQ_LEN, NUM_SENSORS))
        baseline[2, 5, 1] = np.inf
        with pytest.raises(ValueError, match="non-finite"):
            PatternDetector().set_baseline(baseline)

    def test_set_baseline_rejects_short_time_axis(self) -> None:
        too_short = np.zeros((5, PATTERN_MIN_SEQ_LEN - 1, NUM_SENSORS))
        with pytest.raises(ValueError, match="time steps"):
            PatternDetector().set_baseline(too_short)

    def test_set_baseline_accepts_list_input(self, rng) -> None:
        baseline_array = rng.normal(
            BASELINE_LEVEL, BASELINE_NOISE_STD, size=(5, SEQ_LEN, NUM_SENSORS)
        )
        pattern_detector = PatternDetector()
        pattern_detector.set_baseline(baseline_array.tolist())
        assert pattern_detector.baseline is not None
        assert pattern_detector.baseline.num_sequences == 5

    def test_reset_baseline_replaces_statistics(self, rng) -> None:
        pattern_detector = PatternDetector()
        first = rng.normal(0.0, 1.0, size=(5, SEQ_LEN, NUM_SENSORS))
        second = rng.normal(50.0, 1.0, size=(8, SEQ_LEN, NUM_SENSORS))
        pattern_detector.set_baseline(first)
        pattern_detector.set_baseline(second)
        assert pattern_detector.baseline.num_sequences == 8
        assert pattern_detector.baseline.mean == pytest.approx(
            [50.0] * NUM_SENSORS, abs=0.5
        )

    def test_detect_accepts_min_length_sequence(self, detector) -> None:
        """Exactly PATTERN_MIN_SEQ_LEN time steps must pass validation."""
        sequence = np.full((PATTERN_MIN_SEQ_LEN, NUM_SENSORS), BASELINE_LEVEL)
        result = detector.detect(sequence)
        assert result["primary_pattern"] in detector.PATTERN_TYPES
        assert all(0.0 <= s <= 1.0 for s in result["pattern_scores"].values())

    def test_detect_batch_empty_returns_empty_list(self, detector) -> None:
        results = detector.detect_batch(np.zeros((0, SEQ_LEN, NUM_SENSORS)))
        assert results == []

    def test_detect_batch_propagates_invalid_sequence_error(self, detector, rng) -> None:
        sequences = rng.normal(
            BASELINE_LEVEL, BASELINE_NOISE_STD, size=(2, SEQ_LEN, NUM_SENSORS)
        )
        sequences[1, 0, 0] = np.nan
        with pytest.raises(ValueError, match="non-finite"):
            detector.detect_batch(sequences)


# =============================================================================
# QA extension — single-sensor detector (no pairwise structure)
# =============================================================================


class TestQaSingleSensor:
    @staticmethod
    def _single_sensor_baseline(rng) -> "PatternBaseline":
        """Well-formed single-sensor baseline via compute_baseline."""
        from src.models.pattern_scores import PatternBaseline, compute_baseline

        values = rng.normal(0.0, 1.0, size=(20, SEQ_LEN, 1))
        return compute_baseline(values)

    def test_dependency_zero_without_sensor_pairs(self, rng) -> None:
        """With one sensor there are no pairs — dependency must be 0."""
        baseline = self._single_sensor_baseline(rng)
        sequence = rng.normal(0.0, 1.0, size=(SEQ_LEN, 1))
        score, affected = score_dependency(sequence, baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_detect_full_pipeline_with_single_sensor(self, rng) -> None:
        pattern_detector = PatternDetector(sensor_names=["solo"])
        pattern_detector.set_baseline(rng.normal(0.0, 1.0, size=(20, SEQ_LEN, 1)))
        result = pattern_detector.detect(np.zeros((SEQ_LEN, 1)))
        assert result["primary_pattern"] == "drift"
        assert result["affected_sensors"] == ["solo"]

    def test_two_sensor_dependency_through_detect(self, rng) -> None:
        """Minimal 2-sensor pair exercises score_dependency via public detect()."""
        baseline = rng.normal(0.0, 1.0, size=(20, SEQ_LEN, 2))
        shared = _sine_wave()
        baseline[:, :, 0] = shared + rng.normal(0.0, 0.05, size=(20, SEQ_LEN))
        baseline[:, :, 1] = shared + rng.normal(0.0, 0.05, size=(20, SEQ_LEN))

        pattern_detector = PatternDetector(sensor_names=["sensor_a", "sensor_b"])
        pattern_detector.set_baseline(baseline)

        sequence = rng.normal(0.0, 1.0, size=(SEQ_LEN, 2))
        sequence[:, 0] = _sine_wave()
        sequence[:, 1] = -_sine_wave()

        result = pattern_detector.detect(sequence)
        assert result["pattern_scores"]["dependency"] > 0.0
        assert set(result["affected_sensors"]).issubset({"sensor_a", "sensor_b"})
        assert len(result["affected_sensors"]) >= 1


# =============================================================================
# QA extension — baseline spectra edges
# =============================================================================


class TestQaSpectraEdges:
    def test_short_baseline_has_zero_spectra(self, rng) -> None:
        """seq_len below the cyclic minimum -> spectra all zero, no crash."""
        short_len = 10  # >= PATTERN_MIN_SEQ_LEN, < PATTERN_CYCLIC_MIN_SEQ_LEN
        baseline = compute_baseline(
            rng.normal(0.0, 1.0, size=(10, short_len, NUM_SENSORS))
        )
        assert baseline.dominant_power_fractions.tolist() == [0.0] * NUM_SENSORS
        assert baseline.dominant_frequency_bins.tolist() == [0] * NUM_SENSORS

    def test_constant_baseline_channel_has_zero_power_fraction(self, rng) -> None:
        """A constant channel has zero non-DC power — fraction stays 0, finite."""
        sequences = rng.normal(0.0, 1.0, size=(10, SEQ_LEN, NUM_SENSORS))
        sequences[:, :, 2] = 5.0
        baseline = compute_baseline(sequences)
        assert baseline.dominant_power_fractions[2] == 0.0
        assert np.isfinite(baseline.correlations).all()

    def test_constant_channel_at_detect_time_breaks_cycle(self, cyclic_detector) -> None:
        """An exactly-constant light channel has zero power -> cycle broken."""
        sequence = np.full((SEQ_LEN, NUM_SENSORS), BASELINE_LEVEL)
        sequence[:, 3] = 500.0
        score, affected = score_cyclic(sequence, cyclic_detector.baseline)
        assert score == 1.0
        assert affected.tolist() == [3]


# =============================================================================
# QA extension — scoring boundaries and tie-breaks
# =============================================================================


class TestQaScoringBoundaries:
    def test_spike_duration_exactly_at_threshold_counts(self, detector) -> None:
        """Outlier fraction == PATTERN_SPIKE_MAX_DURATION_FRACTION is a spike."""
        max_spike_steps = int(PATTERN_SPIKE_MAX_DURATION_FRACTION * SEQ_LEN)
        sequence = np.full((SEQ_LEN, NUM_SENSORS), BASELINE_LEVEL)
        sequence[:max_spike_steps, 0] = 30.0
        score, affected = score_spike(sequence, detector.baseline)
        assert score > 0.0
        assert affected.tolist() == [0]

    def test_spike_duration_above_threshold_is_not_spike(self, detector) -> None:
        """One step past the max duration -> sustained shift, not a spike."""
        max_spike_steps = int(PATTERN_SPIKE_MAX_DURATION_FRACTION * SEQ_LEN)
        sequence = np.full((SEQ_LEN, NUM_SENSORS), BASELINE_LEVEL)
        sequence[: max_spike_steps + 1, 0] = 30.0
        score, affected = score_spike(sequence, detector.baseline)
        assert score == 0.0
        assert affected.size == 0

    def test_negative_shift_lag_detected(self, correlated_detector, rng) -> None:
        """Lag detection must handle the leading (negative-shift) direction."""
        sequence = rng.normal(BASELINE_LEVEL, 1.0, size=(SEQ_LEN, NUM_SENSORS))
        sequence[:, 0] = _sine_wave()
        sequence[:, 1] = _sine_wave(shift=-3)
        score, affected = score_lag(sequence, correlated_detector.baseline)
        assert score == 1.0
        assert affected.tolist() == [0, 1]

    def test_confidence_scales_with_gap(self, detector) -> None:
        gap = 0.25
        scores = {"a": 0.5, "b": 0.5 - gap, "c": 0.0}
        assert detector._compute_confidence(scores) == pytest.approx(
            gap * PATTERN_CONFIDENCE_GAP_SCALE
        )

    def test_tie_resolves_in_declaration_order(self, detector) -> None:
        """Pinned-high sequence ties drift/saturation/cascade at 1.0 —
        primary must be drift (first in PATTERN_TYPES) with confidence 0."""
        sequence = np.full((SEQ_LEN, NUM_SENSORS), 100.0)
        result = detector.detect(sequence)
        assert result["pattern_scores"]["drift"] == 1.0
        assert result["pattern_scores"]["saturation"] == 1.0
        assert result["primary_pattern"] == "drift"
        assert result["confidence"] == 0.0

    def test_detect_deterministic_across_instances(self, rng) -> None:
        """Same baseline + sequence on fresh instances -> identical results."""
        baseline = rng.normal(
            BASELINE_LEVEL, BASELINE_NOISE_STD, size=(20, SEQ_LEN, NUM_SENSORS)
        )
        sequence = rng.normal(BASELINE_LEVEL, BASELINE_NOISE_STD, size=(SEQ_LEN, NUM_SENSORS))
        sequence[7, 1] = 40.0

        first_detector = PatternDetector()
        first_detector.set_baseline(baseline)
        second_detector = PatternDetector()
        second_detector.set_baseline(baseline)
        assert first_detector.detect(sequence) == second_detector.detect(sequence)

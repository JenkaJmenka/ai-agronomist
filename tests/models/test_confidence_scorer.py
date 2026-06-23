"""
Comprehensive pytest tests for src/models/confidence_scorer.py

Coverage target: ≥90% of ConfidenceScorer.

Test strategy:
- Pure Python/numpy — no torch, no DB, no fixtures from conftest.
- Exact numeric expectations use pytest.approx with absolute tolerance.
- Parametrize non-finite / out-of-range variants to avoid duplication.
- Each class maps to one public method or concern; classes are independent.
"""

import math

import numpy as np
import pytest

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
from src.models.confidence_scorer import ConfidenceScorer

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Standard "perfect anomaly" arguments — every signal should be 1.0.
_PERFECT = dict(
    reconstruction_error=CONFIDENCE_DEFAULT_BASELINE_ERROR * CONFIDENCE_ERROR_RATIO_SCALE,
    pattern_scores={"spike": 1.0},
    ensemble_agreement=CONFIDENCE_ENSEMBLE_SIZE,
    uncertainty=0.0,
    baseline_error=CONFIDENCE_DEFAULT_BASELINE_ERROR,
)

# Standard "no signal" arguments — every signal should be 0.0.
_SILENT = dict(
    reconstruction_error=0.0,
    pattern_scores={"drift": 0.5, "spike": 0.5},  # tie → gap = 0 → clarity = 0
    ensemble_agreement=0,
    uncertainty=1.0,
    baseline_error=CONFIDENCE_DEFAULT_BASELINE_ERROR,
)

# Arbitrary mid-range inputs used for compute_signals numeric spot-checks.
_MIDRANGE = dict(
    reconstruction_error=0.025,           # half baseline → ratio ≈ 0.5 → signal ≈ 1/6
    pattern_scores={"spike": 0.6, "drift": 0.4},  # gap=0.2 → clarity=0.4
    ensemble_agreement=2,                  # 2/4 = 0.5
    uncertainty=0.3,                       # signal = 0.7
    baseline_error=CONFIDENCE_DEFAULT_BASELINE_ERROR,
)


def _make_scorer(**kw) -> ConfidenceScorer:
    """Return a ConfidenceScorer, optionally with custom weights."""
    return ConfidenceScorer(**kw)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    """ConfidenceScorer.__init__ — weights wiring and defaults."""

    def test_default_weights_match_config(self) -> None:
        """No-arg constructor must use CONFIDENCE_SIGNAL_WEIGHTS exactly."""
        scorer = ConfidenceScorer()
        assert scorer.weights == dict(CONFIDENCE_SIGNAL_WEIGHTS)

    def test_custom_valid_weights_are_accepted(self) -> None:
        """Equal-share custom weights must be stored verbatim."""
        equal = {k: 0.25 for k in CONFIDENCE_SIGNAL_WEIGHTS}
        scorer = ConfidenceScorer(weights=equal)
        assert scorer.weights == equal

    def test_signal_names_match_weight_keys(self) -> None:
        """SIGNAL_NAMES tuple must mirror the default weight keys."""
        scorer = ConfidenceScorer()
        assert set(scorer.SIGNAL_NAMES) == set(CONFIDENCE_SIGNAL_WEIGHTS)

    def test_weights_stored_as_dict_copy(self) -> None:
        """Mutating the original mapping must not affect scorer.weights."""
        original = {k: v for k, v in CONFIDENCE_SIGNAL_WEIGHTS.items()}
        scorer = ConfidenceScorer(weights=original)
        original["recon_error"] = 999.0
        assert scorer.weights["recon_error"] == CONFIDENCE_SIGNAL_WEIGHTS["recon_error"]


# ---------------------------------------------------------------------------
# Weight validation
# ---------------------------------------------------------------------------

class TestWeightValidation:
    """_validate_weights — all rejection paths."""

    def test_raises_for_missing_key(self) -> None:
        """Providing fewer keys than required must raise ValueError."""
        bad = {k: v for k, v in CONFIDENCE_SIGNAL_WEIGHTS.items() if k != "uncertainty"}
        with pytest.raises(ValueError, match="keys"):
            ConfidenceScorer(weights=bad)

    def test_raises_for_extra_key(self) -> None:
        """Providing an unexpected key must raise ValueError."""
        bad = {**CONFIDENCE_SIGNAL_WEIGHTS, "bonus": 0.0}
        with pytest.raises(ValueError, match="keys"):
            ConfidenceScorer(weights=bad)

    def test_raises_for_wrong_keys_entirely(self) -> None:
        """Completely wrong key names must raise ValueError."""
        bad = {"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25}
        with pytest.raises(ValueError, match="keys"):
            ConfidenceScorer(weights=bad)

    def test_raises_for_negative_weight(self) -> None:
        """Any weight < 0 must raise ValueError."""
        bad = {**CONFIDENCE_SIGNAL_WEIGHTS, "recon_error": -0.1, "pattern_clarity": 0.5}
        with pytest.raises(ValueError, match="finite and >= 0"):
            ConfidenceScorer(weights=bad)

    @pytest.mark.parametrize("bad_value", [math.inf, -math.inf, math.nan])
    def test_raises_for_non_finite_weight(self, bad_value: float) -> None:
        """Non-finite weights must raise ValueError."""
        bad = {**CONFIDENCE_SIGNAL_WEIGHTS, "recon_error": bad_value}
        with pytest.raises(ValueError, match="finite and >= 0"):
            ConfidenceScorer(weights=bad)

    def test_raises_for_weights_not_summing_to_one(self) -> None:
        """Weights summing to 0.9 (below tolerance) must raise ValueError."""
        bad = {k: v * 0.9 for k, v in CONFIDENCE_SIGNAL_WEIGHTS.items()}
        with pytest.raises(ValueError, match="sum to 1.0"):
            ConfidenceScorer(weights=bad)

    def test_raises_for_weights_summing_above_one(self) -> None:
        """Weights summing to 1.1 must raise ValueError."""
        bad = {k: v * 1.1 for k, v in CONFIDENCE_SIGNAL_WEIGHTS.items()}
        with pytest.raises(ValueError, match="sum to 1.0"):
            ConfidenceScorer(weights=bad)

    def test_accepts_zero_weight_when_sum_is_still_one(self) -> None:
        """A weight of exactly 0.0 is valid as long as the sum is 1.0."""
        w = {
            "recon_error": 0.5,
            "pattern_clarity": 0.5,
            "ensemble_agreement": 0.0,
            "uncertainty": 0.0,
        }
        scorer = ConfidenceScorer(weights=w)
        assert scorer.weights["ensemble_agreement"] == 0.0


# ---------------------------------------------------------------------------
# Input validation (scalar args)
# ---------------------------------------------------------------------------

class TestInputValidation:
    """_validate_inputs — every rejection path for scalar arguments."""

    @pytest.fixture
    def scorer(self) -> ConfidenceScorer:
        return ConfidenceScorer()

    # reconstruction_error
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_raises_for_non_finite_reconstruction_error(
        self, scorer: ConfidenceScorer, bad: float
    ) -> None:
        with pytest.raises(ValueError, match="reconstruction_error"):
            scorer.score(bad, {"drift": 0.5}, 2, 0.1)

    def test_raises_for_negative_reconstruction_error(
        self, scorer: ConfidenceScorer
    ) -> None:
        with pytest.raises(ValueError, match="reconstruction_error"):
            scorer.score(-0.01, {"drift": 0.5}, 2, 0.1)

    # baseline_error
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -0.01])
    def test_raises_for_invalid_baseline_error(
        self, scorer: ConfidenceScorer, bad: float
    ) -> None:
        with pytest.raises(ValueError, match="baseline_error"):
            scorer.score(0.1, {"drift": 0.5}, 2, 0.1, baseline_error=bad)

    # uncertainty
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -0.1])
    def test_raises_for_invalid_uncertainty(
        self, scorer: ConfidenceScorer, bad: float
    ) -> None:
        with pytest.raises(ValueError, match="uncertainty"):
            scorer.score(0.1, {"drift": 0.5}, 2, bad)

    # ensemble_agreement range
    def test_raises_for_ensemble_agreement_above_max(
        self, scorer: ConfidenceScorer
    ) -> None:
        with pytest.raises(ValueError, match="ensemble_agreement"):
            scorer.score(0.1, {"drift": 0.5}, CONFIDENCE_ENSEMBLE_SIZE + 1, 0.1)

    def test_raises_for_negative_ensemble_agreement(
        self, scorer: ConfidenceScorer
    ) -> None:
        with pytest.raises(ValueError, match="ensemble_agreement"):
            scorer.score(0.1, {"drift": 0.5}, -1, 0.1)

    def test_raises_for_non_integer_ensemble_agreement(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Float 2.5 has .is_integer() == False → must raise."""
        with pytest.raises(ValueError, match="ensemble_agreement"):
            scorer.score(0.1, {"drift": 0.5}, 2.5, 0.1)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# pattern_scores validation
# ---------------------------------------------------------------------------

class TestPatternScoresValidation:
    """_validate_pattern_scores — empty and bad-value paths."""

    @pytest.fixture
    def scorer(self) -> ConfidenceScorer:
        return ConfidenceScorer()

    def test_raises_for_empty_pattern_scores(
        self, scorer: ConfidenceScorer
    ) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            scorer.score(0.1, {}, 2, 0.1)

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_raises_for_non_finite_pattern_score(
        self, scorer: ConfidenceScorer, bad: float
    ) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            scorer.score(0.1, {"spike": bad}, 2, 0.1)

    def test_raises_for_pattern_score_above_one(
        self, scorer: ConfidenceScorer
    ) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            scorer.score(0.1, {"spike": 1.01}, 2, 0.1)

    def test_raises_for_pattern_score_below_zero(
        self, scorer: ConfidenceScorer
    ) -> None:
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            scorer.score(0.1, {"spike": -0.01}, 2, 0.1)

    def test_raises_for_non_finite_among_multiple_scores(
        self, scorer: ConfidenceScorer
    ) -> None:
        """NaN in one entry while others are valid must still raise."""
        with pytest.raises(ValueError, match="non-finite"):
            scorer.score(0.1, {"drift": 0.5, "spike": float("nan")}, 2, 0.1)


# ---------------------------------------------------------------------------
# compute_signals — individual signal formulas
# ---------------------------------------------------------------------------

class TestComputeSignals:
    """compute_signals — each signal formula, including edge cases."""

    @pytest.fixture
    def scorer(self) -> ConfidenceScorer:
        return ConfidenceScorer()

    def test_returns_four_named_signals(self, scorer: ConfidenceScorer) -> None:
        signals = scorer.compute_signals(**_MIDRANGE)
        assert set(signals) == {"recon_error", "pattern_clarity", "ensemble_agreement", "uncertainty"}

    def test_all_signals_are_in_zero_one(self, scorer: ConfidenceScorer) -> None:
        signals = scorer.compute_signals(**_MIDRANGE)
        for name, value in signals.items():
            assert 0.0 <= value <= 1.0, f"signal '{name}' = {value} is outside [0, 1]"

    # --- recon_error signal ---

    def test_recon_error_signal_is_zero_for_zero_error(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Zero reconstruction error → signal = 0.0 regardless of baseline."""
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"drift": 0.5},
            ensemble_agreement=2,
            uncertainty=0.0,
        )
        assert signals["recon_error"] == pytest.approx(0.0, abs=1e-6)

    def test_recon_error_signal_is_one_at_full_scale(
        self, scorer: ConfidenceScorer
    ) -> None:
        """At SCALE * baseline error the recon_error signal must reach 1.0."""
        baseline = CONFIDENCE_DEFAULT_BASELINE_ERROR
        full_scale = baseline * CONFIDENCE_ERROR_RATIO_SCALE
        signals = scorer.compute_signals(
            reconstruction_error=full_scale,
            pattern_scores={"drift": 0.5},
            ensemble_agreement=2,
            uncertainty=0.0,
        )
        assert signals["recon_error"] == pytest.approx(1.0, abs=1e-4)

    def test_recon_error_signal_is_capped_at_one(
        self, scorer: ConfidenceScorer
    ) -> None:
        """A very large reconstruction error must not push the signal above 1.0."""
        signals = scorer.compute_signals(
            reconstruction_error=99.9,
            pattern_scores={"drift": 0.5},
            ensemble_agreement=2,
            uncertainty=0.0,
        )
        assert signals["recon_error"] == pytest.approx(1.0, abs=1e-9)

    def test_recon_error_signal_scales_with_custom_baseline(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Doubling the baseline halves the recon_error ratio (before cap)."""
        baseline = 0.1
        recon = baseline  # ratio = 1.0 → signal = 1/3
        signals = scorer.compute_signals(
            reconstruction_error=recon,
            pattern_scores={"drift": 0.5},
            ensemble_agreement=0,
            uncertainty=0.0,
            baseline_error=baseline,
        )
        expected = (recon / (baseline + NUMERICAL_EPSILON)) / CONFIDENCE_ERROR_RATIO_SCALE
        assert signals["recon_error"] == pytest.approx(expected, abs=1e-4)

    # --- pattern_clarity signal ---

    def test_pattern_clarity_single_entry_treats_second_score_as_zero(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Single-entry pattern_scores → second_score = 0.0 by design."""
        top = 0.7
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": top},
            ensemble_agreement=0,
            uncertainty=0.0,
        )
        expected = min(top * PATTERN_CONFIDENCE_GAP_SCALE, 1.0)
        assert signals["pattern_clarity"] == pytest.approx(expected, abs=1e-6)

    def test_pattern_clarity_tie_produces_zero_gap(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Two equal scores → gap = 0 → clarity = 0.0."""
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"drift": 0.5, "spike": 0.5},
            ensemble_agreement=0,
            uncertainty=0.0,
        )
        assert signals["pattern_clarity"] == pytest.approx(0.0, abs=1e-9)

    def test_pattern_clarity_gap_scaled_by_pattern_confidence_gap_scale(
        self, scorer: ConfidenceScorer
    ) -> None:
        """gap * PATTERN_CONFIDENCE_GAP_SCALE must give the clarity value (< 1)."""
        scores = {"spike": 0.6, "drift": 0.4}  # gap = 0.2
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores=scores,
            ensemble_agreement=0,
            uncertainty=0.0,
        )
        expected = min(0.2 * PATTERN_CONFIDENCE_GAP_SCALE, 1.0)
        assert signals["pattern_clarity"] == pytest.approx(expected, abs=1e-6)

    def test_pattern_clarity_is_capped_at_one(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Gap large enough to exceed 1 after scaling must be capped at 1.0."""
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": 1.0, "drift": 0.0},
            ensemble_agreement=0,
            uncertainty=0.0,
        )
        # gap = 1.0, 1.0 * 2.0 = 2.0 → capped at 1.0
        assert signals["pattern_clarity"] == pytest.approx(1.0, abs=1e-9)

    def test_pattern_clarity_with_many_patterns(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Top-2 gap is used even when more than 2 entries are present."""
        # top=0.8, second=0.6, rest don't matter for clarity
        many = {"spike": 0.8, "drift": 0.6, "lag": 0.1, "cascade": 0.0}
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores=many,
            ensemble_agreement=0,
            uncertainty=0.0,
        )
        expected = min((0.8 - 0.6) * PATTERN_CONFIDENCE_GAP_SCALE, 1.0)
        assert signals["pattern_clarity"] == pytest.approx(expected, abs=1e-6)

    # --- ensemble_agreement signal ---

    def test_ensemble_agreement_zero_gives_zero_signal(
        self, scorer: ConfidenceScorer
    ) -> None:
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=0,
            uncertainty=0.0,
        )
        assert signals["ensemble_agreement"] == pytest.approx(0.0)

    def test_ensemble_agreement_full_gives_one_signal(
        self, scorer: ConfidenceScorer
    ) -> None:
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=CONFIDENCE_ENSEMBLE_SIZE,
            uncertainty=0.0,
        )
        assert signals["ensemble_agreement"] == pytest.approx(1.0)

    def test_ensemble_agreement_half_gives_half_signal(
        self, scorer: ConfidenceScorer
    ) -> None:
        half = CONFIDENCE_ENSEMBLE_SIZE // 2
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=half,
            uncertainty=0.0,
        )
        assert signals["ensemble_agreement"] == pytest.approx(half / CONFIDENCE_ENSEMBLE_SIZE)

    def test_ensemble_agreement_one_is_boundary(
        self, scorer: ConfidenceScorer
    ) -> None:
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=1,
            uncertainty=0.0,
        )
        assert signals["ensemble_agreement"] == pytest.approx(1.0 / CONFIDENCE_ENSEMBLE_SIZE)

    # --- uncertainty signal ---

    def test_uncertainty_zero_gives_one_signal(
        self, scorer: ConfidenceScorer
    ) -> None:
        """No uncertainty → maximum confidence contribution."""
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=0,
            uncertainty=0.0,
        )
        assert signals["uncertainty"] == pytest.approx(1.0)

    def test_uncertainty_one_gives_zero_signal(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Uncertainty = 1.0 → uncertainty_signal = 0.0."""
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=0,
            uncertainty=1.0,
        )
        assert signals["uncertainty"] == pytest.approx(0.0)

    def test_uncertainty_above_one_is_capped_to_zero_signal(
        self, scorer: ConfidenceScorer
    ) -> None:
        """uncertainty >= 1.0 is capped; signal cannot go below 0.0."""
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=0,
            uncertainty=5.0,
        )
        assert signals["uncertainty"] == pytest.approx(0.0)

    def test_uncertainty_mid_value(self, scorer: ConfidenceScorer) -> None:
        """Uncertainty of 0.5 → signal = 0.5."""
        signals = scorer.compute_signals(
            reconstruction_error=0.0,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=0,
            uncertainty=0.5,
        )
        assert signals["uncertainty"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# score()
# ---------------------------------------------------------------------------

class TestScore:
    """score() — weighted combination, monotonicity, range."""

    @pytest.fixture
    def scorer(self) -> ConfidenceScorer:
        return ConfidenceScorer()

    def test_score_returns_python_float(self, scorer: ConfidenceScorer) -> None:
        result = scorer.score(**_MIDRANGE)
        assert isinstance(result, float)

    def test_score_is_in_zero_one(self, scorer: ConfidenceScorer) -> None:
        for kwargs in [_PERFECT, _SILENT, _MIDRANGE]:
            s = scorer.score(**kwargs)
            assert 0.0 <= s <= 1.0, f"score={s} is outside [0, 1] for {kwargs}"

    def test_score_is_one_for_perfect_inputs(self, scorer: ConfidenceScorer) -> None:
        """All signals at 1.0 → weighted sum = 1.0."""
        assert scorer.score(**_PERFECT) == pytest.approx(1.0, abs=1e-4)

    def test_score_is_zero_for_silent_inputs(self, scorer: ConfidenceScorer) -> None:
        """All signals at 0.0 → weighted sum = 0.0."""
        assert scorer.score(**_SILENT) == pytest.approx(0.0, abs=1e-9)

    def test_score_is_weighted_combination_of_signals(
        self, scorer: ConfidenceScorer
    ) -> None:
        """score() must equal Σ weight_i * signal_i (manually computed)."""
        signals = scorer.compute_signals(**_MIDRANGE)
        expected = sum(
            CONFIDENCE_SIGNAL_WEIGHTS[k] * v for k, v in signals.items()
        )
        assert scorer.score(**_MIDRANGE) == pytest.approx(expected, abs=1e-9)

    def test_higher_reconstruction_error_increases_score(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Larger recon error (before cap) → higher confidence score."""
        base_kwargs = dict(
            pattern_scores={"spike": 0.5},
            ensemble_agreement=2,
            uncertainty=0.2,
            baseline_error=CONFIDENCE_DEFAULT_BASELINE_ERROR,
        )
        low = scorer.score(reconstruction_error=0.01, **base_kwargs)
        high = scorer.score(reconstruction_error=0.10, **base_kwargs)
        assert high > low

    def test_higher_ensemble_agreement_increases_score(
        self, scorer: ConfidenceScorer
    ) -> None:
        """More detectors agreeing → higher confidence score."""
        base_kwargs = dict(
            reconstruction_error=0.05,
            pattern_scores={"spike": 0.5},
            uncertainty=0.2,
        )
        low = scorer.score(ensemble_agreement=0, **base_kwargs)
        high = scorer.score(ensemble_agreement=CONFIDENCE_ENSEMBLE_SIZE, **base_kwargs)
        assert high > low

    def test_lower_uncertainty_increases_score(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Lower embedding uncertainty → higher confidence score."""
        base_kwargs = dict(
            reconstruction_error=0.05,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=2,
        )
        low = scorer.score(uncertainty=0.9, **base_kwargs)
        high = scorer.score(uncertainty=0.1, **base_kwargs)
        assert high > low

    def test_clearer_dominant_pattern_increases_score(
        self, scorer: ConfidenceScorer
    ) -> None:
        """A more dominant top pattern → larger gap → higher score."""
        base_kwargs = dict(
            reconstruction_error=0.05,
            ensemble_agreement=2,
            uncertainty=0.2,
        )
        low = scorer.score(pattern_scores={"spike": 0.51, "drift": 0.49}, **base_kwargs)
        high = scorer.score(pattern_scores={"spike": 0.9, "drift": 0.1}, **base_kwargs)
        assert high > low

    def test_score_uses_default_baseline_error(self, scorer: ConfidenceScorer) -> None:
        """Calling without baseline_error kwarg must use CONFIDENCE_DEFAULT_BASELINE_ERROR."""
        explicit = scorer.score(
            reconstruction_error=0.05,
            pattern_scores={"spike": 0.8},
            ensemble_agreement=2,
            uncertainty=0.2,
            baseline_error=CONFIDENCE_DEFAULT_BASELINE_ERROR,
        )
        implicit = scorer.score(
            reconstruction_error=0.05,
            pattern_scores={"spike": 0.8},
            ensemble_agreement=2,
            uncertainty=0.2,
        )
        assert explicit == pytest.approx(implicit, abs=1e-12)

    def test_score_with_custom_weights_reflects_weight_shift(self) -> None:
        """Putting all weight on recon_error must make score equal that signal."""
        w = {
            "recon_error": 1.0,
            "pattern_clarity": 0.0,
            "ensemble_agreement": 0.0,
            "uncertainty": 0.0,
        }
        scorer = ConfidenceScorer(weights=w)
        baseline = CONFIDENCE_DEFAULT_BASELINE_ERROR
        recon = baseline  # ratio = 1 → signal ≈ 1/3
        expected_signal = (recon / (baseline + NUMERICAL_EPSILON)) / CONFIDENCE_ERROR_RATIO_SCALE
        result = scorer.score(
            reconstruction_error=recon,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=2,
            uncertainty=0.5,
        )
        assert result == pytest.approx(expected_signal, abs=1e-4)


# ---------------------------------------------------------------------------
# assess()
# ---------------------------------------------------------------------------

class TestAssess:
    """assess() — full contract: all keys, types, and values."""

    @pytest.fixture
    def scorer(self) -> ConfidenceScorer:
        return ConfidenceScorer()

    def test_assess_contains_all_required_keys(
        self, scorer: ConfidenceScorer
    ) -> None:
        result = scorer.assess(**_MIDRANGE)
        required = {"confidence", "is_high_confidence", "is_low_confidence",
                    "confidence_level", "signals"}
        assert required == set(result.keys())

    def test_assess_confidence_is_float_in_zero_one(
        self, scorer: ConfidenceScorer
    ) -> None:
        result = scorer.assess(**_MIDRANGE)
        assert isinstance(result["confidence"], float)
        assert 0.0 <= result["confidence"] <= 1.0

    def test_assess_confidence_matches_score_method(
        self, scorer: ConfidenceScorer
    ) -> None:
        """assess()['confidence'] must equal what score() returns."""
        assert scorer.assess(**_MIDRANGE)["confidence"] == pytest.approx(
            scorer.score(**_MIDRANGE), abs=1e-12
        )

    def test_assess_is_high_confidence_is_bool(
        self, scorer: ConfidenceScorer
    ) -> None:
        result = scorer.assess(**_PERFECT)
        assert isinstance(result["is_high_confidence"], bool)
        assert result["is_high_confidence"] is True

    def test_assess_is_low_confidence_is_bool(
        self, scorer: ConfidenceScorer
    ) -> None:
        result = scorer.assess(**_SILENT)
        assert isinstance(result["is_low_confidence"], bool)
        assert result["is_low_confidence"] is True

    def test_assess_confidence_level_is_string(
        self, scorer: ConfidenceScorer
    ) -> None:
        result = scorer.assess(**_MIDRANGE)
        assert isinstance(result["confidence_level"], str)

    def test_assess_signals_matches_compute_signals(
        self, scorer: ConfidenceScorer
    ) -> None:
        """assess()['signals'] must equal compute_signals() output."""
        assert scorer.assess(**_MIDRANGE)["signals"] == scorer.compute_signals(**_MIDRANGE)

    def test_assess_high_and_low_are_not_both_true_for_mid_range(
        self, scorer: ConfidenceScorer
    ) -> None:
        """A mid-range score cannot be simultaneously high and low."""
        result = scorer.assess(**_MIDRANGE)
        c = result["confidence"]
        # if confidence is neither high nor low, both flags should be False
        if not (c >= CONFIDENCE_HIGH_THRESHOLD) and not (c <= CONFIDENCE_LOW_THRESHOLD):
            assert not result["is_high_confidence"]
            assert not result["is_low_confidence"]

    def test_assess_perfect_inputs_give_very_high_level(
        self, scorer: ConfidenceScorer
    ) -> None:
        result = scorer.assess(**_PERFECT)
        assert result["confidence_level"] == "very_high"


# ---------------------------------------------------------------------------
# high_confidence()
# ---------------------------------------------------------------------------

class TestHighConfidence:
    """high_confidence() — thresholding and validation."""

    @pytest.fixture
    def scorer(self) -> ConfidenceScorer:
        return ConfidenceScorer()

    def test_returns_true_above_default_threshold(
        self, scorer: ConfidenceScorer
    ) -> None:
        assert scorer.high_confidence(CONFIDENCE_HIGH_THRESHOLD + 0.01) is True

    def test_returns_true_at_default_threshold(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Boundary: confidence == threshold → high_confidence (>=)."""
        assert scorer.high_confidence(CONFIDENCE_HIGH_THRESHOLD) is True

    def test_returns_false_below_default_threshold(
        self, scorer: ConfidenceScorer
    ) -> None:
        assert scorer.high_confidence(CONFIDENCE_HIGH_THRESHOLD - 0.01) is False

    def test_custom_threshold_is_respected(self, scorer: ConfidenceScorer) -> None:
        custom = 0.5
        assert scorer.high_confidence(0.5, threshold=custom) is True
        assert scorer.high_confidence(0.49, threshold=custom) is False

    @pytest.mark.parametrize("bad", [-0.1, 1.01, float("nan"), float("inf")])
    def test_raises_for_invalid_confidence_value(
        self, scorer: ConfidenceScorer, bad: float
    ) -> None:
        with pytest.raises(ValueError, match="confidence"):
            scorer.high_confidence(bad)


# ---------------------------------------------------------------------------
# low_confidence()
# ---------------------------------------------------------------------------

class TestLowConfidence:
    """low_confidence() — thresholding and validation."""

    @pytest.fixture
    def scorer(self) -> ConfidenceScorer:
        return ConfidenceScorer()

    def test_returns_true_below_default_threshold(
        self, scorer: ConfidenceScorer
    ) -> None:
        assert scorer.low_confidence(CONFIDENCE_LOW_THRESHOLD - 0.01) is True

    def test_returns_true_at_default_threshold(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Boundary: confidence == threshold → low_confidence (<=)."""
        assert scorer.low_confidence(CONFIDENCE_LOW_THRESHOLD) is True

    def test_returns_false_above_default_threshold(
        self, scorer: ConfidenceScorer
    ) -> None:
        assert scorer.low_confidence(CONFIDENCE_LOW_THRESHOLD + 0.01) is False

    def test_custom_threshold_is_respected(self, scorer: ConfidenceScorer) -> None:
        custom = 0.6
        assert scorer.low_confidence(0.6, threshold=custom) is True
        assert scorer.low_confidence(0.61, threshold=custom) is False

    @pytest.mark.parametrize("bad", [-0.1, 1.01, float("nan"), float("inf")])
    def test_raises_for_invalid_confidence_value(
        self, scorer: ConfidenceScorer, bad: float
    ) -> None:
        with pytest.raises(ValueError, match="confidence"):
            scorer.low_confidence(bad)


# ---------------------------------------------------------------------------
# confidence_level()
# ---------------------------------------------------------------------------

class TestConfidenceLevel:
    """confidence_level() — label assignment and boundary values."""

    @pytest.fixture
    def scorer(self) -> ConfidenceScorer:
        return ConfidenceScorer()

    @pytest.mark.parametrize("confidence, expected", [
        (1.0,  "very_high"),
        (0.95, "very_high"),
        (0.85, "very_high"),   # exactly at the bound (>=)
        (0.84, "high"),
        (0.80, "high"),
        (0.75, "high"),        # exactly at the bound
        (0.74, "medium"),
        (0.65, "medium"),
        (0.60, "medium"),      # exactly at the bound
        (0.59, "low"),
        (0.50, "low"),
        (0.40, "low"),         # exactly at the bound
        (0.39, "very_low"),
        (0.20, "very_low"),
        (0.0,  "very_low"),
    ])
    def test_confidence_level_label(
        self, scorer: ConfidenceScorer, confidence: float, expected: str
    ) -> None:
        assert scorer.confidence_level(confidence) == expected

    def test_all_config_level_bounds_are_covered(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Every label from CONFIDENCE_LEVEL_BOUNDS and CONFIDENCE_MIN_LEVEL appears."""
        level_labels = {label for _, label in CONFIDENCE_LEVEL_BOUNDS}
        level_labels.add(CONFIDENCE_MIN_LEVEL)
        returned_labels = {
            scorer.confidence_level(c)
            for c in [0.9, 0.8, 0.65, 0.5, 0.1]
        }
        assert returned_labels == level_labels

    @pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan"), float("inf")])
    def test_raises_for_out_of_range_or_non_finite_confidence(
        self, scorer: ConfidenceScorer, bad: float
    ) -> None:
        with pytest.raises(ValueError, match="confidence"):
            scorer.confidence_level(bad)


# ---------------------------------------------------------------------------
# End-to-end / integration
# ---------------------------------------------------------------------------

class TestEndToEnd:
    """Full pipeline: scorer.assess() called with realistic agronomist data."""

    @pytest.fixture
    def scorer(self) -> ConfidenceScorer:
        return ConfidenceScorer()

    def test_typical_anomaly_scenario(self, scorer: ConfidenceScorer) -> None:
        """
        Simulated sensor spike: high recon error, spike clearly dominant,
        3 of 4 detectors agree, low latent uncertainty → should be high confidence.
        """
        result = scorer.assess(
            reconstruction_error=0.12,
            pattern_scores={"spike": 0.85, "drift": 0.10, "lag": 0.05},
            ensemble_agreement=3,
            uncertainty=0.08,
        )
        assert result["confidence"] > CONFIDENCE_HIGH_THRESHOLD
        assert result["is_high_confidence"] is True
        assert result["is_low_confidence"] is False
        assert result["confidence_level"] in {"very_high", "high"}

    def test_noise_scenario(self, scorer: ConfidenceScorer) -> None:
        """
        Borderline noise: low recon error, no clear pattern, only 1 detector,
        moderate uncertainty → should be low confidence.
        """
        result = scorer.assess(
            reconstruction_error=0.02,
            pattern_scores={"drift": 0.35, "spike": 0.33, "lag": 0.32},
            ensemble_agreement=1,
            uncertainty=0.8,
        )
        assert result["confidence"] < CONFIDENCE_HIGH_THRESHOLD
        assert result["is_high_confidence"] is False

    def test_zero_reconstruction_error_anchors_low_score(
        self, scorer: ConfidenceScorer
    ) -> None:
        """
        Zero recon error (anomaly smaller than normal data) should lower overall score
        even if other signals are moderate.
        """
        result_zero = scorer.assess(
            reconstruction_error=0.0,
            pattern_scores={"spike": 0.7},
            ensemble_agreement=2,
            uncertainty=0.2,
        )
        result_nonzero = scorer.assess(
            reconstruction_error=0.10,
            pattern_scores={"spike": 0.7},
            ensemble_agreement=2,
            uncertainty=0.2,
        )
        assert result_zero["confidence"] < result_nonzero["confidence"]

    def test_single_pattern_entry_does_not_crash(
        self, scorer: ConfidenceScorer
    ) -> None:
        """Single-entry pattern_scores must work without IndexError."""
        result = scorer.assess(
            reconstruction_error=0.05,
            pattern_scores={"cascade": 0.9},
            ensemble_agreement=2,
            uncertainty=0.15,
        )
        assert "confidence" in result

    def test_uncertainty_at_exactly_one_gives_zero_signal(
        self, scorer: ConfidenceScorer
    ) -> None:
        """uncertainty == 1.0 → uncertainty_signal = 0.0 (boundary, not clamped)."""
        signals = scorer.compute_signals(
            reconstruction_error=0.05,
            pattern_scores={"spike": 0.8},
            ensemble_agreement=3,
            uncertainty=1.0,
        )
        assert signals["uncertainty"] == pytest.approx(0.0)

    def test_all_boundary_ensemble_values_are_accepted(
        self, scorer: ConfidenceScorer
    ) -> None:
        """ensemble_agreement = 0 and CONFIDENCE_ENSEMBLE_SIZE must both pass validation."""
        for boundary in [0, CONFIDENCE_ENSEMBLE_SIZE]:
            scorer.score(
                reconstruction_error=0.05,
                pattern_scores={"spike": 0.5},
                ensemble_agreement=boundary,
                uncertainty=0.1,
            )

    def test_custom_baseline_error_affects_recon_signal_proportionally(
        self, scorer: ConfidenceScorer
    ) -> None:
        """With baseline 10× larger, the same recon error gives ~10× smaller signal."""
        recon = 0.05
        signals_small_baseline = scorer.compute_signals(
            reconstruction_error=recon,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=2,
            uncertainty=0.0,
            baseline_error=0.01,
        )
        signals_large_baseline = scorer.compute_signals(
            reconstruction_error=recon,
            pattern_scores={"spike": 0.5},
            ensemble_agreement=2,
            uncertainty=0.0,
            baseline_error=0.1,
        )
        assert signals_small_baseline["recon_error"] > signals_large_baseline["recon_error"]

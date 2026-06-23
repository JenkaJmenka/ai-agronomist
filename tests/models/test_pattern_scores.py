"""Tests for score_spike() in pattern_scores — spec-compliant detection paths."""

from __future__ import annotations

import numpy as np
import pytest

from src.models.pattern_scores import (
    PatternBaseline,
    compute_baseline,
    score_spike,
)


@pytest.fixture()
def rng() -> np.random.Generator:
    return np.random.default_rng(42)


def _make_baseline(rng: np.random.Generator) -> PatternBaseline:
    """Baseline from 50 normal sequences with tight noise (realistic)."""
    loc = np.array([25.0, 65.0, 400.0, 500.0])
    # Noise much smaller than velocity limits so normal data never triggers primary
    scale = np.array([0.2, 0.5, 5.0, 20.0])
    sequences = rng.normal(loc=loc, scale=scale, size=(50, 20, 4))
    return compute_baseline(sequences)


@pytest.fixture()
def baseline(rng: np.random.Generator) -> PatternBaseline:
    return _make_baseline(rng)


def _smooth_sequence(
    rng: np.random.Generator,
) -> np.ndarray:
    """Create a 20×4 sequence with noise well below velocity limits."""
    loc = np.array([25.0, 65.0, 400.0, 500.0])
    scale = np.array([0.2, 0.5, 5.0, 20.0])
    return rng.normal(loc=loc, scale=scale, size=(20, 4))


class TestPrimaryPathVelocityReversal:
    """Primary detection: velocity + reversal (SCADA spec section 1.1)."""

    def test_velocity_reversal_detected_even_with_low_z(
        self, baseline: PatternBaseline,
    ) -> None:
        """A spike with |Δx| > Δ_max and reversal IS detected even when
        z-score < 3.0 (would be missed by fallback path)."""
        # Deterministic: constant sensor readings, then spike and revert.
        seq = np.full((20, 4), [25.0, 65.0, 400.0, 500.0])
        # Temperature limit = 2.0°C/step. Create a 3.0°C spike that reverses.
        seq[10, 0] = 28.0   # +3.0 from 25.0 → exceeds velocity limit 2.0
        # step 11 reverts to 25.0 → reversal confirmed
        score, affected = score_spike(seq, baseline)
        assert score == 1.0
        assert 0 in affected

    def test_velocity_without_reversal_not_primary(
        self, baseline: PatternBaseline,
    ) -> None:
        """Step change (exceeds velocity but no reversal) does NOT trigger
        the primary path."""
        # Deterministic: constant then step up, stay up.
        seq = np.full((20, 4), [25.0, 65.0, 400.0, 500.0])
        seq[10:, 0] = 28.0  # permanent step up, no reversal
        score, affected = score_spike(seq, baseline)
        # Primary path should NOT fire: diff[9]=+3.0, diff[10]=0.0
        # 3.0 * 0.0 = 0.0 which is NOT < 0, so no reversal.
        assert score < 1.0 or 0 not in affected

    def test_multiple_sensors_detected(
        self, baseline: PatternBaseline,
    ) -> None:
        """Multiple sensors with velocity+reversal are all reported."""
        seq = np.full((20, 4), [25.0, 65.0, 400.0, 500.0])
        # Spike temperature (limit 2.0): +3.0 then revert
        seq[5, 0] = 28.0
        # Spike humidity (limit 5.0): +7.0 then revert
        seq[5, 1] = 72.0
        score, affected = score_spike(seq, baseline)
        assert score == 1.0
        assert 0 in affected
        assert 1 in affected

    def test_sequence_too_short_for_primary(
        self, baseline: PatternBaseline,
    ) -> None:
        """With < 3 timesteps, primary path cannot work; falls through."""
        seq = np.array([[25.0, 65.0, 400.0, 500.0], [30.0, 65.0, 400.0, 500.0]])
        score, _ = score_spike(seq, baseline)
        # No crash, primary skipped
        assert isinstance(score, float)


class TestPrimaryPathNoVelocityLimit:
    """Sensors without a velocity limit skip the primary path gracefully."""

    def test_sensor_without_velocity_limit_uses_fallback(
        self, baseline: PatternBaseline, rng: np.random.Generator,
    ) -> None:
        """When all sensors lack velocity limits, primary path is skipped entirely.
        score_spike must fall through to z-score fallback without error."""
        from unittest.mock import patch
        seq = _smooth_sequence(rng)
        # Patch VELOCITY_LIMITS to empty so every vel_limit is None (line 146 branch)
        with patch("src.models.pattern_scores._SPIKE_VELOCITY_LIMITS", {}):
            score, affected = score_spike(seq, baseline)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0


class TestFallbackZScore:
    """Fallback z-score path for sensors without velocity limits."""

    def test_normal_sequence_no_spike(
        self, baseline: PatternBaseline, rng: np.random.Generator,
    ) -> None:
        """Normal data yields score 0.0 (with realistic tight noise)."""
        seq = _smooth_sequence(rng)
        score, affected = score_spike(seq, baseline)
        # With tight noise, neither primary nor fallback should fire
        assert score == 0.0 or affected.size == 0

    def test_z_score_fallback_score_in_range(
        self, baseline: PatternBaseline, rng: np.random.Generator,
    ) -> None:
        """Score is always in [0, 1]."""
        seq = _smooth_sequence(rng)
        score, _ = score_spike(seq, baseline)
        assert 0.0 <= score <= 1.0

    def test_return_types(
        self, baseline: PatternBaseline, rng: np.random.Generator,
    ) -> None:
        """score_spike returns (float, np.ndarray)."""
        seq = _smooth_sequence(rng)
        score, affected = score_spike(seq, baseline)
        assert isinstance(score, float)
        assert isinstance(affected, np.ndarray)

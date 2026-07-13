"""Tests for MultivariateMahalanobisDetector (P3 joint-implausibility check)."""

from __future__ import annotations

import numpy as np
import pytest

from src.models.multivariate_mahalanobis_detector import (
    MultivariateMahalanobisDetector,
)


def _normal_window(seed: int = 0, n: int = 20) -> np.ndarray:
    """Correlated, physically plausible readings around greenhouse norms."""
    rng = np.random.default_rng(seed)
    temp = 25.0 + rng.normal(0.0, 0.3, n)
    humidity = 65.0 - 1.5 * (temp - 25.0) + rng.normal(0.0, 0.5, n)
    co2 = 400.0 + rng.normal(0.0, 8.0, n)
    light = 500.0 + rng.normal(0.0, 15.0, n)
    return np.column_stack([temp, humidity, co2, light])


@pytest.fixture
def detector() -> MultivariateMahalanobisDetector:
    return MultivariateMahalanobisDetector()


class TestNormalWindows:
    def test_correlated_normal_window_not_flagged(self, detector) -> None:
        flags = detector.detect(_normal_window())
        assert not any(flags)

    def test_near_constant_window_not_flagged(self, detector) -> None:
        """Noise-floor regularization: tiny jitter must not alarm."""
        rng = np.random.default_rng(1)
        seq = np.tile([25.0, 65.0, 400.0, 500.0], (20, 1)) + rng.normal(
            0.0, 0.01, (20, 4)
        )
        assert not any(detector.detect(seq))


class TestJointOutliers:
    def test_jointly_implausible_timestep_flagged(self, detector) -> None:
        """Each value is in range, but the combination breaks correlation."""
        seq = _normal_window()
        # Hot AND humid AND CO2 crash at one timestep — individually all
        # within physical bounds, jointly far outside the fitted cloud.
        seq[10] = [29.5, 79.0, 320.0, 500.0]
        flags = detector.detect(seq)
        assert flags[10] is True
        assert sum(flags) == 1, "only the implausible timestep flags"

    def test_single_channel_spike_flagged(self, detector) -> None:
        seq = _normal_window(seed=2)
        seq[5, 0] = 29.9  # strong temperature-only deviation
        assert detector.detect(seq)[5] is True


class TestEdgeCases:
    def test_short_window_returns_all_false(self, detector) -> None:
        assert detector.detect(_normal_window(n=6)) == [False] * 6

    def test_nan_window_returns_all_false(self, detector) -> None:
        seq = _normal_window()
        seq[3, 2] = np.nan
        assert detector.detect(seq) == [False] * 20

    def test_rejects_wrong_dimensionality(self, detector) -> None:
        with pytest.raises(ValueError):
            detector.detect(np.zeros(20))

    def test_rejects_nonpositive_threshold(self) -> None:
        with pytest.raises(ValueError):
            MultivariateMahalanobisDetector(chi2_threshold=0.0)

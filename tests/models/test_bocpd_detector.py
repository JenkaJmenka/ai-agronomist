"""Tests for BOCPDDetector (P3 level-shift change-point detection)."""

from __future__ import annotations

import numpy as np
import pytest

from src.models.bocpd_detector import BOCPDDetector


@pytest.fixture
def detector() -> BOCPDDetector:
    return BOCPDDetector()


class TestLevelShifts:
    def test_step_change_flagged_near_shift(self, detector) -> None:
        rng = np.random.default_rng(0)
        series = np.concatenate(
            [22.0 + rng.normal(0, 0.1, 30), 27.0 + rng.normal(0, 0.1, 30)]
        )
        flags = detector.detect(series)
        shift_zone = flags[30:36]
        assert any(shift_zone), "change-point must be flagged shortly after t=30"
        assert not any(flags[:30]), "no flags before the shift"

    def test_second_shift_also_flagged(self, detector) -> None:
        rng = np.random.default_rng(3)
        series = np.concatenate(
            [
                22.0 + rng.normal(0, 0.1, 25),
                26.0 + rng.normal(0, 0.1, 25),
                22.0 + rng.normal(0, 0.1, 25),
            ]
        )
        flags = detector.detect(series)
        assert any(flags[25:31])
        assert any(flags[50:56])


class TestStableSeries:
    def test_constant_series_not_flagged(self, detector) -> None:
        assert not any(detector.detect(np.full(60, 22.0)))

    def test_noisy_stationary_series_not_flagged(self, detector) -> None:
        rng = np.random.default_rng(1)
        series = 25.0 + rng.normal(0, 0.2, 60)
        assert not any(detector.detect(series))

    def test_slow_smooth_trend_not_flagged(self, detector) -> None:
        """A gentle ramp is drift (CUSUM territory), not a change-point."""
        series = np.linspace(22.0, 22.5, 60)
        flags = detector.detect(series)
        assert sum(flags) <= 1


class TestEdgeCases:
    def test_short_series_returns_all_false(self, detector) -> None:
        assert detector.detect(np.array([1.0, 2.0, 3.0])) == [False, False, False]

    def test_nan_series_returns_all_false(self, detector) -> None:
        series = np.full(30, 22.0)
        series[10] = np.nan
        assert detector.detect(series) == [False] * 30

    def test_rejects_bad_hazard(self) -> None:
        with pytest.raises(ValueError):
            BOCPDDetector(hazard_lambda=1.0)

    def test_rejects_bad_min_run(self) -> None:
        with pytest.raises(ValueError):
            BOCPDDetector(min_run=1)

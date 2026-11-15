"""Detector contract tests: CUSUM/EMA guard, noise baseline, Mahalanobis.

B.  _install_saved_cusum_ema: legacy state installs neither; physical state
    installs both; filled cache slot is not replaced.
D.  resolve_noise_baseline: never below fixed floor; ignores non-finite input.
    CUSUMDriftDetector.update_and_reset: returns True on alarm, clears accumulators.
    EMABaselineEstimator.from_dict: absent min_std_by_sensor restores empty.
    MultivariateMahalanobisDetector.score_last_against_history: returns False
    with insufficient history.
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pytest

from src.models.cusum_drift_detector import CUSUMDriftDetector, EMABaselineEstimator
from src.models.multivariate_mahalanobis_detector import MultivariateMahalanobisDetector
from src.models.noise_detector import NOISE_BASELINE_VARIANCE, resolve_noise_baseline


# ---------------------------------------------------------------------------
# B — _install_saved_cusum_ema (via ComprehensiveInferencePipeline)
# ---------------------------------------------------------------------------

class TestInstallSavedCusumEma:
    """Legacy CUSUM state guard on ComprehensiveInferencePipeline."""

    def _make_pipeline(self):
        """Build a minimal pipeline with empty caches."""
        from src.api.services.comprehensive_pipeline import (
            ComprehensiveInferencePipeline,
        )
        p = ComprehensiveInferencePipeline.__new__(ComprehensiveInferencePipeline)
        p._cusum_cache = {}
        p._ema_cache = {}
        return p

    def test_legacy_state_empty_min_std_installs_neither(self):
        """ema_baseline_state with empty min_std_by_sensor installs nothing."""
        pipe = self._make_pipeline()
        state: Dict[str, Any] = {
            "ema_baseline_state": {
                "alpha": 0.01,
                "min_std": 1e-6,
                "min_std_by_sensor": {},
                "mean": {"temperature": 22.0},
                "var": {"temperature": 0.1},
            },
            "cusum_state": {
                "k": 0.25,
                "h": 4.0,
                "s_pos": {},
                "s_neg": {},
            },
        }
        pipe._install_saved_cusum_ema("tenant-legacy", state)
        assert "tenant-legacy" not in pipe._cusum_cache
        assert "tenant-legacy" not in pipe._ema_cache

    def test_legacy_state_missing_min_std_installs_neither(self):
        """ema_baseline_state with absent min_std_by_sensor installs nothing."""
        pipe = self._make_pipeline()
        state: Dict[str, Any] = {
            "ema_baseline_state": {
                "alpha": 0.01,
                "min_std": 1e-6,
                "mean": {"temperature": 22.0},
                "var": {"temperature": 0.1},
            },
            "cusum_state": {
                "k": 0.25,
                "h": 4.0,
                "s_pos": {},
                "s_neg": {},
            },
        }
        pipe._install_saved_cusum_ema("tenant-legacy2", state)
        assert "tenant-legacy2" not in pipe._cusum_cache
        assert "tenant-legacy2" not in pipe._ema_cache

    def test_physical_state_installs_both(self):
        """State written by for_physical_series().to_dict() installs both."""
        pipe = self._make_pipeline()
        ema = EMABaselineEstimator.for_physical_series()
        # Feed a few samples so the EMA has real state.
        ema.update("temperature", 22.0)
        ema.update("humidity", 60.0)
        ema_dict = ema.to_dict()

        cusum = CUSUMDriftDetector()
        cusum_dict = cusum.to_dict()

        state: Dict[str, Any] = {
            "ema_baseline_state": ema_dict,
            "cusum_state": cusum_dict,
        }
        pipe._install_saved_cusum_ema("tenant-physical", state)
        assert "tenant-physical" in pipe._cusum_cache
        assert "tenant-physical" in pipe._ema_cache
        assert isinstance(pipe._cusum_cache["tenant-physical"], CUSUMDriftDetector)
        assert isinstance(pipe._ema_cache["tenant-physical"], EMABaselineEstimator)

    def test_filled_cache_slot_not_replaced(self):
        """An existing cache entry is not overwritten by _install_saved_cusum_ema."""
        pipe = self._make_pipeline()
        original_cusum = CUSUMDriftDetector(k=0.5, h=5.0)
        original_ema = EMABaselineEstimator(alpha=0.05)
        pipe._cusum_cache["tenant-x"] = original_cusum
        pipe._ema_cache["tenant-x"] = original_ema

        ema = EMABaselineEstimator.for_physical_series()
        ema.update("temperature", 22.0)
        state: Dict[str, Any] = {
            "ema_baseline_state": ema.to_dict(),
            "cusum_state": CUSUMDriftDetector().to_dict(),
        }
        pipe._install_saved_cusum_ema("tenant-x", state)

        # Should still be the original objects.
        assert pipe._cusum_cache["tenant-x"] is original_cusum
        assert pipe._ema_cache["tenant-x"] is original_ema


# ---------------------------------------------------------------------------
# D — resolve_noise_baseline
# ---------------------------------------------------------------------------

class TestResolveNoiseBaseline:
    """resolve_noise_baseline never returns below the fixed floor."""

    def test_empty_calibration_returns_fixed(self):
        result = resolve_noise_baseline()
        for sensor, fixed_val in NOISE_BASELINE_VARIANCE.items():
            assert result[sensor] == fixed_val

    def test_calibration_below_floor_keeps_floor(self):
        # temperature fixed floor is 0.01; pass a smaller calibration.
        result = resolve_noise_baseline({"temperature": 0.001})
        assert result["temperature"] >= NOISE_BASELINE_VARIANCE["temperature"]

    def test_calibration_above_floor_takes_calibration(self):
        result = resolve_noise_baseline({"temperature": 99.0})
        assert result["temperature"] == 99.0

    def test_non_finite_input_ignored(self):
        result = resolve_noise_baseline({
            "temperature": float("inf"),
            "humidity": float("nan"),
        })
        assert result["temperature"] == NOISE_BASELINE_VARIANCE["temperature"]
        assert result["humidity"] == NOISE_BASELINE_VARIANCE["humidity"]

    def test_non_positive_input_ignored(self):
        result = resolve_noise_baseline({"temperature": -1.0, "co2": 0.0})
        assert result["temperature"] == NOISE_BASELINE_VARIANCE["temperature"]
        assert result["co2"] == NOISE_BASELINE_VARIANCE["co2"]


# ---------------------------------------------------------------------------
# D — CUSUMDriftDetector.update_and_reset
# ---------------------------------------------------------------------------

class TestCUSUMUpdateAndReset:
    """update_and_reset returns True on alarm and clears accumulators."""

    def test_alarm_and_clear(self):
        # k=0.25, h=4.0 (defaults). A large positive residual should
        # push s_pos above h in one step: s_pos = 0 + 10.0 - 0.25 = 9.75 > 4.0
        det = CUSUMDriftDetector(k=0.25, h=4.0)
        result = det.update_and_reset("temperature", 10.0)
        assert result is True
        # After alarm, accumulators should be cleared.
        assert det._s_pos["temperature"] == 0.0
        assert det._s_neg["temperature"] == 0.0

    def test_no_alarm_on_small_residual(self):
        det = CUSUMDriftDetector(k=0.25, h=4.0)
        result = det.update_and_reset("temperature", 0.5)
        assert result is False
        # Accumulators should NOT be cleared (no alarm).
        assert det._s_pos["temperature"] == 0.25  # 0.5 - 0.25


# ---------------------------------------------------------------------------
# D — EMABaselineEstimator.from_dict without min_std_by_sensor
# ---------------------------------------------------------------------------

class TestEMAFromDict:
    """from_dict on a payload without min_std_by_sensor restores empty floor map."""

    def test_absent_min_std_by_sensor_restores_empty(self):
        payload = {
            "alpha": 0.01,
            "min_std": 1e-6,
            "mean": {},
            "var": {},
        }
        ema = EMABaselineEstimator.from_dict(payload)
        # The internal floor map should be empty.
        assert ema._min_std_by_sensor == {}

    def test_present_min_std_by_sensor_restores(self):
        ema_physical = EMABaselineEstimator.for_physical_series()
        payload = ema_physical.to_dict()
        restored = EMABaselineEstimator.from_dict(payload)
        assert restored._min_std_by_sensor == ema_physical._min_std_by_sensor
        assert len(restored._min_std_by_sensor) > 0


# ---------------------------------------------------------------------------
# D — MultivariateMahalanobisDetector.score_last_against_history
# ---------------------------------------------------------------------------

class TestMahalanobisInsufficientHistory:
    """Returns False when fewer than 2*n_sensors finite history rows."""

    def test_too_few_history_rows(self):
        det = MultivariateMahalanobisDetector()
        window = np.array([[22.0, 60.0, 400.0, 500.0]])  # 1 row, 4 sensors
        # Need at least 2*4=8 finite rows. Provide only 5.
        history = np.random.default_rng(42).normal(
            loc=[22, 60, 400, 500], scale=[1, 2, 10, 50], size=(5, 4)
        )
        assert det.score_last_against_history(window, history) is False

    def test_sufficient_history_does_not_return_false_for_normal(self):
        det = MultivariateMahalanobisDetector()
        rng = np.random.default_rng(42)
        history = rng.normal(
            loc=[22, 60, 400, 500], scale=[1, 2, 10, 50], size=(20, 4)
        )
        # A normal window drawn from the same distribution should not alarm.
        window = rng.normal(
            loc=[22, 60, 400, 500], scale=[1, 2, 10, 50], size=(1, 4)
        )
        result = det.score_last_against_history(window, history)
        # We only check it does NOT crash and returns a bool (not necessarily False).
        assert isinstance(result, bool)

    def test_non_finite_in_history_reduces_effective_rows(self):
        det = MultivariateMahalanobisDetector()
        window = np.array([[22.0, 60.0, 400.0, 500.0]])
        # 10 rows, but inject NaN in rows 0-5 so only 4 finite rows remain
        # (below the 2*4=8 threshold).
        history = np.full((10, 4), [22.0, 60.0, 400.0, 500.0])
        history[:6, 0] = float("nan")
        assert det.score_last_against_history(window, history) is False

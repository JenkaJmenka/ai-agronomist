"""Tests for CUSUMDriftDetector and EMABaselineEstimator.

Uses document Table 4 verification dataset and standard CUSUM properties.
"""

import numpy as np
import pytest

from src.models.cusum_drift_detector import CUSUMDriftDetector, EMABaselineEstimator


class TestCUSUMDriftDetector:
    """Document Table 4 adapted: CUSUM with standardized residuals."""

    def test_no_drift(self):
        cusum = CUSUMDriftDetector(k=0.5, h=5.0)
        for _ in range(100):
            assert cusum.update("temp", 0.0) is False

    def test_positive_drift_alarm(self):
        """Feed increasing standardised residuals; alarm must fire."""
        cusum = CUSUMDriftDetector(k=0.5, h=5.0)
        alarmed = False
        for r in [0.0] * 5 + [1.5] * 20:
            if cusum.update("temp", r):
                alarmed = True
                break
        assert alarmed

    def test_accumulation_below_threshold(self):
        cusum = CUSUMDriftDetector(k=0.5, h=5.0)
        assert cusum.update("temp", 0.6) is False
        assert cusum.update("temp", 0.0) is False

    def test_negative_drift(self):
        cusum = CUSUMDriftDetector(k=0.5, h=5.0)
        alarmed = False
        for r in [0.0] * 5 + [-1.5] * 20:
            if cusum.update("temp", r):
                alarmed = True
                break
        assert alarmed

    def test_reset_clears_accumulators(self):
        cusum = CUSUMDriftDetector(k=0.5, h=5.0)
        for _ in range(20):
            cusum.update("temp", 2.0)
        cusum.reset("temp")
        state = cusum.to_dict()
        assert state["s_pos"]["temp"] == 0.0
        assert state["s_neg"]["temp"] == 0.0

    def test_production_sensitive_params(self):
        """K=0.25, H=4.0 — more sensitive to small shifts."""
        cusum = CUSUMDriftDetector(k=0.25, h=4.0)
        alarmed = False
        for r in [0.0] * 5 + [0.8] * 30:
            if cusum.update("temp", r):
                alarmed = True
                break
        assert alarmed

    def test_serialization_roundtrip(self):
        cusum = CUSUMDriftDetector(k=0.5, h=5.0)
        for r in [1.0, 0.5, 0.8]:
            cusum.update("temp", r)
        state = cusum.to_dict()
        restored = CUSUMDriftDetector.from_dict(state)
        assert restored.k == 0.5
        assert restored.h == 5.0
        orig_state = cusum.to_dict()
        rest_state = restored.to_dict()
        assert abs(orig_state["s_pos"]["temp"] - rest_state["s_pos"]["temp"]) < 1e-10

    def test_invalid_k_raises(self):
        with pytest.raises(ValueError):
            CUSUMDriftDetector(k=-1.0, h=5.0)

    def test_invalid_h_raises(self):
        with pytest.raises(ValueError):
            CUSUMDriftDetector(k=0.5, h=-1.0)


class TestEMABaselineEstimator:
    """Verify EMA baseline estimation for CUSUM input."""

    def test_first_observation_zero_residual(self):
        ema = EMABaselineEstimator(alpha=0.1)
        residual, mean = ema.update("temp", 22.0)
        assert residual == 0.0
        assert mean == 22.0

    def test_stable_series_near_zero_residuals(self):
        """Stable series should produce small residuals."""
        ema = EMABaselineEstimator(alpha=0.1)
        for v in [22.0] * 50:
            residual, _ = ema.update("temp", v)
        assert abs(residual) < 0.1

    def test_drift_produces_positive_residuals(self):
        """Upward drift should produce positive residuals."""
        ema = EMABaselineEstimator(alpha=0.01)
        for v in [22.0] * 20:
            ema.update("temp", v)
        residual, _ = ema.update("temp", 25.0)
        assert residual > 0

    def test_serialization_roundtrip(self):
        ema = EMABaselineEstimator(alpha=0.05)
        for v in [22.0, 22.1, 21.9, 22.0]:
            ema.update("temp", v)
        state = ema.to_dict()
        restored = EMABaselineEstimator.from_dict(state)
        assert restored._alpha == 0.05

    def test_invalid_alpha_raises(self):
        with pytest.raises(ValueError):
            EMABaselineEstimator(alpha=0.0)
        with pytest.raises(ValueError):
            EMABaselineEstimator(alpha=1.5)

    def test_cusum_integration(self):
        """EMA + CUSUM end-to-end: drift must trigger alarm."""
        ema = EMABaselineEstimator(alpha=0.1)
        cusum = CUSUMDriftDetector(k=0.5, h=5.0)

        for v in [22.0] * 30:
            residual, _ = ema.update("temp", v)
            cusum.update("temp", residual)

        alarmed = False
        for i in range(50):
            v = 22.0 + 0.3 * i
            residual, _ = ema.update("temp", v)
            if cusum.update("temp", residual):
                alarmed = True
                break

        assert alarmed, "CUSUM should alarm on persistent upward drift"

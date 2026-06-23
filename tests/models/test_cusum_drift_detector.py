"""Tests for CUSUMDriftDetector (SCADA spec section 1.3 — calibration drift)."""

from __future__ import annotations

import pytest

from src.models.cusum_drift_detector import CUSUMDriftDetector


@pytest.fixture()
def cusum() -> CUSUMDriftDetector:
    return CUSUMDriftDetector(k=0.5, h=5.0)


class TestConstruction:
    """Initialisation and parameter validation."""

    def test_default_parameters(self) -> None:
        detector = CUSUMDriftDetector()
        assert detector.k == 0.5
        assert detector.h == 5.0

    def test_custom_parameters(self) -> None:
        detector = CUSUMDriftDetector(k=1.0, h=10.0)
        assert detector.k == 1.0
        assert detector.h == 10.0

    def test_negative_k_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            CUSUMDriftDetector(k=-0.1)

    def test_negative_h_raises(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            CUSUMDriftDetector(h=-1.0)

    def test_zero_k_allowed(self) -> None:
        detector = CUSUMDriftDetector(k=0.0)
        assert detector.k == 0.0

    def test_zero_h_allowed(self) -> None:
        detector = CUSUMDriftDetector(h=0.0)
        assert detector.h == 0.0


class TestUpdate:
    """Core CUSUM accumulation and alarm logic."""

    def test_no_alarm_on_small_residuals(self, cusum: CUSUMDriftDetector) -> None:
        """Residuals within allowance k should not trigger alarm."""
        for _ in range(100):
            assert cusum.update("temperature", 0.3) is False

    def test_alarm_on_sustained_positive_drift(self, cusum: CUSUMDriftDetector) -> None:
        """Persistent positive residuals should eventually trigger alarm."""
        alarmed = False
        for _ in range(100):
            if cusum.update("temperature", 2.0):
                alarmed = True
                break
        assert alarmed

    def test_alarm_on_sustained_negative_drift(self, cusum: CUSUMDriftDetector) -> None:
        """Persistent negative residuals should eventually trigger alarm."""
        alarmed = False
        for _ in range(100):
            if cusum.update("humidity", -2.0):
                alarmed = True
                break
        assert alarmed

    def test_independent_sensors(self, cusum: CUSUMDriftDetector) -> None:
        """Accumulators are per-sensor — drifting one should not affect another."""
        for _ in range(20):
            cusum.update("temperature", 2.0)
            cusum.update("humidity", 0.0)
        assert "humidity" not in cusum.alarmed_sensors()

    def test_positive_accumulator_resets_on_negative_residuals(self) -> None:
        """S+ cannot go below zero — it resets, preventing alarm from noise."""
        detector = CUSUMDriftDetector(k=0.5, h=5.0)
        detector.update("co2", 2.0)  # S+ = 1.5
        detector.update("co2", -5.0)  # S+ = max(0, 1.5 - 5.0 - 0.5) = 0
        # Now S+ is reset to 0, confirm via to_dict
        state = detector.to_dict()
        assert state["s_pos"]["co2"] == 0.0

    def test_steps_to_alarm_with_constant_residual(self) -> None:
        """With k=0.5, h=5.0 and residual=1.5: increment = 1.5-0.5 = 1.0 per step.
        S+ reaches 5.0 at step 5, alarm fires (>5.0 at step 6)."""
        detector = CUSUMDriftDetector(k=0.5, h=5.0)
        alarm_step = None
        for step in range(1, 20):
            if detector.update("temp", 1.5):
                alarm_step = step
                break
        assert alarm_step == 6  # S+ at step 5 = 5.0 (not >), step 6 = 6.0 (>5.0)


class TestReset:
    """Accumulator reset after alarm acknowledgement."""

    def test_reset_clears_accumulators(self, cusum: CUSUMDriftDetector) -> None:
        for _ in range(20):
            cusum.update("temperature", 2.0)
        assert "temperature" in cusum.alarmed_sensors()
        cusum.reset("temperature")
        assert "temperature" not in cusum.alarmed_sensors()

    def test_reset_unknown_sensor_is_safe(self, cusum: CUSUMDriftDetector) -> None:
        """Resetting a sensor that was never updated should not raise."""
        cusum.reset("nonexistent")
        assert cusum.alarmed_sensors() == []


class TestAlarmedSensors:
    """Listing sensors currently in alarm state."""

    def test_empty_initially(self, cusum: CUSUMDriftDetector) -> None:
        assert cusum.alarmed_sensors() == []

    def test_lists_only_alarmed(self, cusum: CUSUMDriftDetector) -> None:
        for _ in range(20):
            cusum.update("temperature", 2.0)
            cusum.update("humidity", 0.1)
        alarmed = cusum.alarmed_sensors()
        assert "temperature" in alarmed
        assert "humidity" not in alarmed

    def test_multiple_alarmed_sensors(self, cusum: CUSUMDriftDetector) -> None:
        for _ in range(20):
            cusum.update("temperature", 2.0)
            cusum.update("co2", -3.0)
        alarmed = cusum.alarmed_sensors()
        assert "temperature" in alarmed
        assert "co2" in alarmed


class TestSerialisation:
    """Round-trip serialisation via to_dict / from_dict."""

    def test_round_trip_preserves_state(self, cusum: CUSUMDriftDetector) -> None:
        for _ in range(5):
            cusum.update("temperature", 1.5)
        state = cusum.to_dict()
        restored = CUSUMDriftDetector.from_dict(state)
        assert restored.k == cusum.k
        assert restored.h == cusum.h
        assert restored.to_dict() == state

    def test_from_dict_with_defaults(self) -> None:
        """Missing fields in the dict should fall back to defaults."""
        restored = CUSUMDriftDetector.from_dict({})
        assert restored.k == 0.5
        assert restored.h == 5.0
        assert restored.alarmed_sensors() == []

    def test_from_dict_restores_alarm_state(self) -> None:
        detector = CUSUMDriftDetector(k=0.5, h=5.0)
        for _ in range(20):
            detector.update("temperature", 2.0)
        assert "temperature" in detector.alarmed_sensors()
        restored = CUSUMDriftDetector.from_dict(detector.to_dict())
        assert "temperature" in restored.alarmed_sensors()

    def test_to_dict_format(self, cusum: CUSUMDriftDetector) -> None:
        cusum.update("temperature", 1.0)
        state = cusum.to_dict()
        assert set(state.keys()) == {"k", "h", "s_pos", "s_neg"}
        assert isinstance(state["s_pos"], dict)
        assert isinstance(state["s_neg"], dict)
        assert "temperature" in state["s_pos"]

    def test_from_dict_negative_k_raises(self) -> None:
        with pytest.raises(ValueError, match="k and h must be non-negative"):
            CUSUMDriftDetector.from_dict({"k": -1.0})

    def test_from_dict_invalid_s_pos_type(self) -> None:
        with pytest.raises(TypeError, match="s_pos must be a dictionary"):
            CUSUMDriftDetector.from_dict({"s_pos": [1, 2, 3]})

    def test_from_dict_invalid_s_neg_type(self) -> None:
        with pytest.raises(TypeError, match="s_neg must be a dictionary"):
            CUSUMDriftDetector.from_dict({"s_neg": "not a dict"})

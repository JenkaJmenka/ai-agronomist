"""Tests for the stub CUSUMDriftDetector class inside src/models/drift_detector.py.

The CUSUMDriftDetector in drift_detector.py is a *different* class from the
real implementation in cusum_drift_detector.py — it is a stub with a frozen
public interface (update / alarmed_sensors / reset / to_dict / from_dict) while
the underlying CUSUM recurrence is left as a TODO.

Targets (lines 178, 186-187, 203-214 in drift_detector.py):
- alarmed_sensors() — line 178
- reset()           — lines 186-187
- from_dict()       — lines 203-214
"""
from __future__ import annotations

import pytest

from src.config import CUSUM_H_SIGMA, CUSUM_K_SIGMA
from src.models.drift_detector import CUSUMDriftDetector


@pytest.fixture()
def stub() -> CUSUMDriftDetector:
    return CUSUMDriftDetector(k=0.5, h=5.0)


# ---------------------------------------------------------------------------
# alarmed_sensors() — line 178
# ---------------------------------------------------------------------------

class TestAlarmedSensors:
    def test_empty_initially(self, stub: CUSUMDriftDetector) -> None:
        """alarmed_sensors() returns an empty list at construction."""
        assert stub.alarmed_sensors() == []

    def test_returns_list_type(self, stub: CUSUMDriftDetector) -> None:
        assert isinstance(stub.alarmed_sensors(), list)

    def test_stays_empty_after_updates(self, stub: CUSUMDriftDetector) -> None:
        """Stub update() always returns False — no sensor ever enters alarm state."""
        for _ in range(50):
            stub.update("temperature", 100.0)
        assert stub.alarmed_sensors() == []

    def test_returns_new_list_each_call(self, stub: CUSUMDriftDetector) -> None:
        """Each call should return an independent list (not the internal reference)."""
        a = stub.alarmed_sensors()
        b = stub.alarmed_sensors()
        assert a is not b


# ---------------------------------------------------------------------------
# reset() — lines 186-187
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_known_sensor_does_not_raise(self, stub: CUSUMDriftDetector) -> None:
        stub.update("temperature", 3.0)
        stub.reset("temperature")  # should not raise
        assert "temperature" not in stub.alarmed_sensors()

    def test_reset_unknown_sensor_is_safe(self, stub: CUSUMDriftDetector) -> None:
        stub.reset("nonexistent_sensor")
        assert stub.alarmed_sensors() == []

    def test_reset_clears_accumulated_stats(self, stub: CUSUMDriftDetector) -> None:
        """After reset, to_dict should show zeroed stats for that sensor."""
        for _ in range(10):
            stub.update("co2", 2.0)
        stub.reset("co2")
        state = stub.to_dict()
        assert state["stats"]["co2"] == [0.0, 0.0]

    def test_reset_preserves_other_sensors_stats(self, stub: CUSUMDriftDetector) -> None:
        """Resetting one sensor must not disturb another sensor's state."""
        for _ in range(5):
            stub.update("temperature", 1.5)
            stub.update("humidity", 0.8)
        stub.reset("temperature")
        state = stub.to_dict()
        assert "humidity" in state["stats"]
        assert state["stats"]["temperature"] == [0.0, 0.0]

    def test_reset_multiple_times_is_safe(self, stub: CUSUMDriftDetector) -> None:
        stub.update("light", 5.0)
        stub.reset("light")
        stub.reset("light")  # second reset should be a no-op
        assert stub.alarmed_sensors() == []


# ---------------------------------------------------------------------------
# from_dict() — lines 203-214
# ---------------------------------------------------------------------------

class TestFromDict:
    def test_round_trip_empty_state(self, stub: CUSUMDriftDetector) -> None:
        """from_dict(to_dict()) should reproduce identical state."""
        state = stub.to_dict()
        restored = CUSUMDriftDetector.from_dict(state)
        assert restored.k == stub.k
        assert restored.h == stub.h
        assert restored.to_dict() == state

    def test_from_dict_uses_default_k_when_missing(self) -> None:
        restored = CUSUMDriftDetector.from_dict({"h": 8.0})
        assert restored.k == float(CUSUM_K_SIGMA)
        assert restored.h == 8.0

    def test_from_dict_uses_default_h_when_missing(self) -> None:
        restored = CUSUMDriftDetector.from_dict({"k": 1.0})
        assert restored.k == 1.0
        assert restored.h == float(CUSUM_H_SIGMA)

    def test_from_dict_empty_dict_uses_all_defaults(self) -> None:
        restored = CUSUMDriftDetector.from_dict({})
        assert restored.k == float(CUSUM_K_SIGMA)
        assert restored.h == float(CUSUM_H_SIGMA)
        assert restored.alarmed_sensors() == []

    def test_from_dict_restores_stats(self) -> None:
        stub = CUSUMDriftDetector(k=1.0, h=10.0)
        for _ in range(5):
            stub.update("humidity", 0.5)
        state = stub.to_dict()
        restored = CUSUMDriftDetector.from_dict(state)
        assert restored.k == 1.0
        assert restored.h == 10.0
        assert restored.to_dict() == state

    def test_from_dict_restores_alarms_list(self) -> None:
        """_alarms list is restored verbatim from the dict."""
        state = {
            "k": 0.5,
            "h": 5.0,
            "stats": {"temperature": [6.0, 0.0]},
            "alarms": ["temperature"],
        }
        restored = CUSUMDriftDetector.from_dict(state)
        assert "temperature" in restored.alarmed_sensors()

    def test_from_dict_skips_invalid_stat_pairs(self) -> None:
        """Stat entries that are not 2-element sequences are silently dropped."""
        state = {
            "k": 0.5,
            "h": 5.0,
            "stats": {"bad_sensor": [1.0], "good_sensor": [1.0, 2.0]},
            "alarms": [],
        }
        restored = CUSUMDriftDetector.from_dict(state)
        restored_stats = restored.to_dict()["stats"]
        assert "bad_sensor" not in restored_stats
        assert "good_sensor" in restored_stats

    def test_to_dict_format_contains_expected_keys(self, stub: CUSUMDriftDetector) -> None:
        stub.update("temperature", 1.0)
        state = stub.to_dict()
        assert set(state.keys()) == {"k", "h", "stats", "alarms"}
        assert isinstance(state["stats"], dict)
        assert isinstance(state["alarms"], list)
        assert "temperature" in state["stats"]

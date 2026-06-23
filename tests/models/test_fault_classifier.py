"""Tests for FaultClassifier — focus on POLLING_DROPOUT priority rule.

The existing classifier tests are in tests/unit/test_scada_detectors.py.
This module specifically tests the Priority A additions:
- FaultType.POLLING_DROPOUT enum value
- POLLING_DROPOUT as the highest-priority classification rule
"""

from __future__ import annotations

import pytest

from src.models.fault_classifier import FaultClassifier, FaultType


class TestPollingDropoutEnum:
    """FaultType.POLLING_DROPOUT enum membership."""

    def test_enum_value(self) -> None:
        assert FaultType.POLLING_DROPOUT.value == "polling_dropout"

    def test_enum_is_member(self) -> None:
        assert hasattr(FaultType, "POLLING_DROPOUT")

    def test_enum_accessible_by_value(self) -> None:
        assert FaultType("polling_dropout") is FaultType.POLLING_DROPOUT


class TestPollingDropoutClassification:
    """POLLING_DROPOUT is the highest-priority rule in the classifier."""

    def test_polling_dropout_returns_correct_value(self) -> None:
        result = FaultClassifier.classify(
            pattern_type="polling_dropout", sensor_stats={}, bias_flag=False
        )
        assert result == "polling_dropout"

    def test_polling_dropout_beats_bias_flag(self) -> None:
        """POLLING_DROPOUT should win even when bias_flag is True."""
        result = FaultClassifier.classify(
            pattern_type="polling_dropout", sensor_stats={}, bias_flag=True
        )
        assert result == "polling_dropout"

    def test_polling_dropout_beats_spike(self) -> None:
        """When pattern_type is exactly 'polling_dropout', it should not
        fall through to the 'spike' rule."""
        result = FaultClassifier.classify(
            pattern_type="polling_dropout",
            sensor_stats={"noise_level": "high"},
            bias_flag=False,
        )
        assert result == "polling_dropout"

    def test_polling_dropout_case_sensitive(self) -> None:
        """The rule uses exact match, not substring/lowered match."""
        result = FaultClassifier.classify(
            pattern_type="POLLING_DROPOUT", sensor_stats={}, bias_flag=False
        )
        # Should NOT match — falls through to UNKNOWN
        assert result != "polling_dropout"


class TestExistingRulesUnchanged:
    """Verify that existing classification rules still work after modification."""

    def test_bias_flag_still_works(self) -> None:
        result = FaultClassifier.classify(
            pattern_type="drift", sensor_stats={}, bias_flag=True
        )
        assert result == "bias_offset"

    def test_stuck_still_works(self) -> None:
        result = FaultClassifier.classify(
            pattern_type="stuck", sensor_stats={}, bias_flag=False
        )
        assert result == "stuck_fault"

    def test_saturation_still_works(self) -> None:
        result = FaultClassifier.classify(
            pattern_type="saturation", sensor_stats={}, bias_flag=False
        )
        assert result == "stuck_fault"

    def test_spike_still_works(self) -> None:
        result = FaultClassifier.classify(
            pattern_type="spike", sensor_stats={}, bias_flag=False
        )
        assert result == "spike_fault"

    def test_drift_still_works(self) -> None:
        result = FaultClassifier.classify(
            pattern_type="drift", sensor_stats={}, bias_flag=False
        )
        assert result == "calibration_drift"

    def test_high_noise_still_works(self) -> None:
        result = FaultClassifier.classify(
            pattern_type="unknown_pattern",
            sensor_stats={"noise_level": "high"},
            bias_flag=False,
        )
        assert result == "accuracy_decline"

    def test_unknown_fallthrough(self) -> None:
        result = FaultClassifier.classify(
            pattern_type="gibberish", sensor_stats={}, bias_flag=False
        )
        assert result == "unknown"


class TestPriorityOrdering:
    """Verify the full priority chain with polling_dropout at the top."""

    def test_full_priority_chain(self) -> None:
        """polling_dropout > bias_flag > stuck > spike > drift > noise > unknown."""
        # 1. polling_dropout beats everything
        assert (
            FaultClassifier.classify("polling_dropout", {}, bias_flag=True)
            == "polling_dropout"
        )
        # 2. bias_flag beats content-based rules
        assert (
            FaultClassifier.classify("spike", {}, bias_flag=True) == "bias_offset"
        )
        # 3. stuck/saturation beats spike
        assert (
            FaultClassifier.classify("stuck_spike", {}, bias_flag=False)
            == "stuck_fault"
        )
        # 4. spike beats drift
        assert (
            FaultClassifier.classify("spike_drift", {}, bias_flag=False)
            == "spike_fault"
        )

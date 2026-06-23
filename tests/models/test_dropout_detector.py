"""Tests for DropoutDetector (SCADA spec section 1.5 — consecutive polling dropout)."""

from __future__ import annotations

import math

import pytest

from src.models.dropout_detector import DropoutDetector


@pytest.fixture()
def detector() -> DropoutDetector:
    return DropoutDetector()


def _null_row() -> dict[str, None]:
    return {"temperature": None, "humidity": None, "co2": None, "light": None}


def _valid_row() -> dict[str, float]:
    return {"temperature": 25.0, "humidity": 60.0, "co2": 400.0, "light": 500.0}


def _single_null_row() -> dict[str, float | None]:
    """One sensor null, rest valid — should still count as ψ=1."""
    return {"temperature": 25.0, "humidity": None, "co2": 400.0, "light": 500.0}


class TestConsecutiveDetection:
    """Spec: P consecutive steps with any null/NaN triggers alarm."""

    def test_p_consecutive_all_null_triggers(self, detector: DropoutDetector) -> None:
        """P consecutive all-null rows → alarm."""
        rows = [_null_row() for _ in range(5)] + [_valid_row() for _ in range(15)]
        assert detector.detect(rows) is True

    def test_p_consecutive_single_null_triggers(self, detector: DropoutDetector) -> None:
        """P consecutive rows with ONE sensor null → alarm (ANY, not ALL)."""
        rows = [_single_null_row() for _ in range(5)] + [_valid_row() for _ in range(15)]
        assert detector.detect(rows) is True

    def test_scattered_nulls_no_trigger(self, detector: DropoutDetector) -> None:
        """Nulls scattered (never P consecutive) → no alarm."""
        rows: list[dict] = []
        for i in range(20):
            # Alternate: null, valid, valid, null, valid, valid...
            if i % 3 == 0:
                rows.append(_single_null_row())
            else:
                rows.append(_valid_row())
        assert detector.detect(rows) is False

    def test_p_minus_1_consecutive_no_trigger(self, detector: DropoutDetector) -> None:
        """P-1 consecutive nulls → no alarm."""
        rows = [_null_row() for _ in range(4)] + [_valid_row() for _ in range(16)]
        assert detector.detect(rows) is False

    def test_exactly_p_consecutive_in_middle(self, detector: DropoutDetector) -> None:
        """P consecutive nulls buried in the middle → alarm."""
        rows = (
            [_valid_row() for _ in range(5)]
            + [_null_row() for _ in range(5)]
            + [_valid_row() for _ in range(10)]
        )
        assert detector.detect(rows) is True

    def test_more_than_p_consecutive(self, detector: DropoutDetector) -> None:
        """More than P consecutive → alarm (early exit)."""
        rows = [_null_row() for _ in range(10)] + [_valid_row() for _ in range(10)]
        assert detector.detect(rows) is True


class TestEdgeCases:
    """Edge cases for input handling."""

    def test_empty_sequence_returns_false(self, detector: DropoutDetector) -> None:
        assert detector.detect([]) is False

    def test_all_valid_returns_false(self, detector: DropoutDetector) -> None:
        rows = [_valid_row() for _ in range(20)]
        assert detector.detect(rows) is False

    def test_all_null_returns_true(self, detector: DropoutDetector) -> None:
        rows = [_null_row() for _ in range(20)]
        assert detector.detect(rows) is True

    def test_single_null_row_not_enough(self, detector: DropoutDetector) -> None:
        """1 consecutive null < P=5 → no alarm."""
        assert detector.detect([_null_row()]) is False

    def test_empty_dict_treated_as_null(self, detector: DropoutDetector) -> None:
        """An empty dict counts as a null row."""
        rows: list[dict] = [{}] * 5 + [_valid_row()] * 15
        assert detector.detect(rows) is True

    def test_nan_values_count_as_null(self, detector: DropoutDetector) -> None:
        """NaN sensor values are treated as null (ψ=1)."""
        nan_row = {"temperature": float("nan"), "humidity": 60.0, "co2": 400.0, "light": 500.0}
        rows = [nan_row for _ in range(5)] + [_valid_row() for _ in range(15)]
        assert detector.detect(rows) is True

    def test_mixed_null_nan_consecutive(self, detector: DropoutDetector) -> None:
        """Mixed None and NaN across consecutive steps."""
        rows = [
            {"temperature": None, "humidity": 60.0, "co2": 400.0, "light": 500.0},
            {"temperature": 25.0, "humidity": float("nan"), "co2": 400.0, "light": 500.0},
            {"temperature": 25.0, "humidity": 60.0, "co2": None, "light": 500.0},
            {"temperature": 25.0, "humidity": 60.0, "co2": 400.0, "light": float("nan")},
            {"temperature": None, "humidity": None, "co2": None, "light": None},
        ] + [_valid_row() for _ in range(15)]
        assert detector.detect(rows) is True


class TestThreshold:
    """Verify the CONSECUTIVE_THRESHOLD constant."""

    def test_threshold_value(self) -> None:
        assert DropoutDetector.CONSECUTIVE_THRESHOLD == 5

    def test_threshold_accessible_on_instance(self, detector: DropoutDetector) -> None:
        assert detector.CONSECUTIVE_THRESHOLD == 5

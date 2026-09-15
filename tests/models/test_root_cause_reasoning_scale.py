"""Regression tests: reasoning text must never quote a non-physical humidity.

Production bug: ``_format_rule_description`` read humidity from the normalized
model-input array, so a row measuring 55.9 %RH was reported to operators as
"Humidity at 1.0%".  Any percentage in the reasoning must agree with the row's
physical humidity, or the text must quote no percentage at all.
"""

import re

import numpy as np
import pytest

from src.config import (
    HUMIDITY_RANGE,
    RH_ABSOLUTE_MAX_PERCENT,
    RH_CRIT_PERCENT,
    SENSOR_NAMES,
)
from src.models.data_utils import SENSOR_RANGES
from src.models.root_cause_analyzer import RootCauseAnalyzer

# The rule under test — saturation attributed to humidity.
SATURATION_PATTERN = "saturation"
HUMIDITY_ATTRIBUTION = {
    "humidity": 0.8,
    "temperature": 0.1,
    "co2": 0.05,
    "light": 0.05,
}

_PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _quoted_percentages(reasoning: str) -> list[float]:
    """Every percentage the operator-facing text claims."""
    return [float(m) for m in _PERCENT_PATTERN.findall(reasoning)]


def _physical_sequence(humidity_percent: float) -> np.ndarray:
    """A physical-unit window whose last humidity reading is given."""
    midpoints = [(low + high) / 2.0 for low, high in SENSOR_RANGES]
    sequence = np.tile(np.array(midpoints, dtype=np.float32), (20, 1))
    sequence[:, SENSOR_NAMES.index("humidity")] = humidity_percent
    return sequence


def _normalized_sequence(physical: np.ndarray) -> np.ndarray:
    """The model input the pipeline actually scores — the buggy read source."""
    ranges = np.array(SENSOR_RANGES, dtype=np.float32)
    mins, spans = ranges[:, 0], ranges[:, 1] - ranges[:, 0]
    return (physical - mins) / spans


@pytest.fixture
def analyzer() -> RootCauseAnalyzer:
    return RootCauseAnalyzer()


class TestReasoningQuotesPhysicalHumidity:
    @pytest.mark.parametrize(
        "humidity_percent", [38.0, 55.87, 72.4, 80.0, 96.0, 99.5]
    )
    def test_quoted_percentage_matches_physical_humidity(
        self, analyzer: RootCauseAnalyzer, humidity_percent: float
    ) -> None:
        physical = _physical_sequence(humidity_percent)
        result = analyzer.analyze(
            SATURATION_PATTERN,
            HUMIDITY_ATTRIBUTION,
            _normalized_sequence(physical),
            physical_sequence=physical,
        )

        for quoted in _quoted_percentages(result["reasoning"]):
            assert quoted == pytest.approx(humidity_percent, abs=0.05), (
                f"reasoning quoted {quoted}% for a {humidity_percent}% row: "
                f"{result['reasoning']!r}"
            )

    def test_production_case_does_not_report_normalized_value(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        """55.87 %RH was reported as '1.0%' — the normalized read."""
        physical = _physical_sequence(55.87)
        result = analyzer.analyze(
            SATURATION_PATTERN,
            HUMIDITY_ATTRIBUTION,
            _normalized_sequence(physical),
            physical_sequence=physical,
        )
        assert "55.9%" in result["reasoning"]
        assert "1.0%" not in result["reasoning"]

    def test_no_percentage_claimed_without_a_physical_window(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        """A wrong number is worse than no number."""
        physical = _physical_sequence(96.0)
        result = analyzer.analyze(
            SATURATION_PATTERN,
            HUMIDITY_ATTRIBUTION,
            _normalized_sequence(physical),
        )
        assert _quoted_percentages(result["reasoning"]) == []
        assert result["root_cause"] == "humidity_limit_exceeded"


class TestReasoningClaimsMatchTheReading:
    def test_ordinary_humidity_does_not_claim_elevated_moisture_load(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        physical = _physical_sequence(55.87)
        result = analyzer.analyze(
            SATURATION_PATTERN,
            HUMIDITY_ATTRIBUTION,
            _normalized_sequence(physical),
            physical_sequence=physical,
        )
        assert "elevated moisture load" not in result["reasoning"]

    def test_critical_humidity_claims_elevated_moisture_load(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        physical = _physical_sequence(RH_CRIT_PERCENT + 1.0)
        result = analyzer.analyze(
            SATURATION_PATTERN,
            HUMIDITY_ATTRIBUTION,
            _normalized_sequence(physical),
            physical_sequence=physical,
        )
        assert "elevated moisture load" in result["reasoning"]

    @pytest.mark.parametrize(
        "humidity_percent", [55.87, RH_CRIT_PERCENT + 1.0]
    )
    def test_at_maximum_is_never_claimed_below_absolute_max(
        self, analyzer: RootCauseAnalyzer, humidity_percent: float
    ) -> None:
        physical = _physical_sequence(humidity_percent)
        result = analyzer.analyze(
            SATURATION_PATTERN,
            HUMIDITY_ATTRIBUTION,
            _normalized_sequence(physical),
            physical_sequence=physical,
        )
        assert "at maximum" not in result["reasoning"]

    def test_at_maximum_is_claimed_at_absolute_max(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        physical = _physical_sequence(RH_ABSOLUTE_MAX_PERCENT)
        result = analyzer.analyze(
            SATURATION_PATTERN,
            HUMIDITY_ATTRIBUTION,
            _normalized_sequence(physical),
            physical_sequence=physical,
        )
        assert "at maximum" in result["reasoning"]


class TestNormalizedInputIsNotMistakenForHumidity:
    def test_normalized_window_passed_as_physical_is_rejected(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        """Out-of-range %RH (normalized reads 0–1.7) must not be quoted.

        HUMIDITY_RANGE spans %RH, so a normalized 1.67 corresponds to the
        top of the configured band — the formatter must refuse it because
        it is a ratio, not a percentage.
        """
        physical = _physical_sequence(HUMIDITY_RANGE[1] + 25.0)
        result = analyzer.analyze(
            SATURATION_PATTERN,
            HUMIDITY_ATTRIBUTION,
            _normalized_sequence(physical),
            physical_sequence=physical * 100.0,  # implausible %RH
        )
        assert _quoted_percentages(result["reasoning"]) == []

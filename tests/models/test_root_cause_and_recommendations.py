"""
Tests for RootCauseAnalyzer and RecommendationEngine.

Coverage: root cause diagnosis, recommendation generation, input validation,
severity/priority classification, urgency, technician requirements, and
downtime estimates.

Test strategy:
- Pure Python/numpy — no torch, no DB, no conftest fixtures.
- Seeded RNG (``np.random.default_rng(42)``) for determinism.
- Each class maps to one concern; classes are independent.
"""

import numpy as np
import pytest

from src.config import (
    RECOMMENDATION_MAX_ACTIONS,
    RECOMMENDATION_URGENT_CAUSES,
    ROOT_CAUSE_ATTRIBUTION_THRESHOLD,
    ROOT_CAUSE_UNKNOWN_CONFIDENCE,
)
from src.models.recommendation_engine import RecommendationEngine
from src.models.root_cause_analyzer import RootCauseAnalyzer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def analyzer() -> RootCauseAnalyzer:
    """Default analyzer with standard 4-sensor config."""
    return RootCauseAnalyzer()


@pytest.fixture
def recommender() -> RecommendationEngine:
    """Default recommendation engine."""
    return RecommendationEngine()


@pytest.fixture
def normal_sequence() -> np.ndarray:
    """A (20, 4) sensor sequence of random normal readings."""
    rng = np.random.default_rng(42)
    return rng.standard_normal((20, 4))


@pytest.fixture
def constant_sequence() -> np.ndarray:
    """A (20, 4) sensor sequence with near-zero variance (stuck sensors)."""
    return np.ones((20, 4)) * 25.0


# ---------------------------------------------------------------------------
# RootCauseAnalyzer — ventilation failure
# ---------------------------------------------------------------------------


class TestVentilationFailure:
    """Dependency pattern with light + temperature affected."""

    def test_detects_ventilation_failure(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "light": 0.3,
            "temperature": 0.4,
            "humidity": 0.2,
            "co2": 0.1,
        }
        result = analyzer.analyze("dependency", attribution, normal_sequence)
        assert result["root_cause"] == "ventilation_failure"
        assert result["confidence"] == 0.85

    def test_ventilation_urgency_is_urgent(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "light": 0.3,
            "temperature": 0.4,
            "humidity": 0.2,
            "co2": 0.1,
        }
        result = analyzer.analyze("dependency", attribution, normal_sequence)
        assert result["urgency"] == "urgent"


# ---------------------------------------------------------------------------
# RootCauseAnalyzer — drift / sensor failure
# ---------------------------------------------------------------------------


class TestDriftSensor:
    """Drift pattern with single sensor and near-zero variance."""

    def test_drift_temperature_detects_sensor_failure(
        self, analyzer: RootCauseAnalyzer, constant_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.9,
            "humidity": 0.05,
            "co2": 0.03,
            "light": 0.02,
        }
        result = analyzer.analyze("drift", attribution, constant_sequence)
        assert result["root_cause"] == "sensor_failure"
        assert result["confidence"] == 0.75

    def test_drift_has_zero_variance_evidence(
        self, analyzer: RootCauseAnalyzer, constant_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.9,
            "humidity": 0.05,
            "co2": 0.03,
            "light": 0.02,
        }
        result = analyzer.analyze("drift", attribution, constant_sequence)
        assert any(
            "variance" in e.lower()
            for e in result["supporting_evidence"]
        )


# ---------------------------------------------------------------------------
# RootCauseAnalyzer — cascade patterns
# ---------------------------------------------------------------------------


class TestCascadeMultiSensor:
    """Cascade pattern with 3+ sensors (multi_sensor) and cooling_related."""

    def test_cascade_3_sensors_detects_power_issue(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        """3 affected sensors (not temp+humidity) → multi_sensor rule."""
        attribution = {
            "co2": 0.4,
            "light": 0.3,
            "humidity": 0.2,
            "temperature": 0.1,
        }
        result = analyzer.analyze("cascade", attribution, normal_sequence)
        assert result["root_cause"] == "power_supply_issue"

    def test_cascade_cooling_related(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        """Temperature + humidity affected → cooling_related → compressor."""
        attribution = {
            "temperature": 0.4,
            "humidity": 0.4,
            "co2": 0.1,
            "light": 0.1,
        }
        result = analyzer.analyze("cascade", attribution, normal_sequence)
        assert result["root_cause"] == "compressor_failure"
        assert result["confidence"] == 0.80
        assert len(result["supporting_evidence"]) >= 1
        assert any(
            "temperature" in e.lower() or "humidity" in e.lower() or "co2" in e.lower()
            for e in result["supporting_evidence"]
        )


# ---------------------------------------------------------------------------
# RootCauseAnalyzer — spike patterns
# ---------------------------------------------------------------------------


class TestSpikePatterns:
    """Spike patterns for temperature and CO2."""

    def test_spike_temperature(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.8,
            "humidity": 0.1,
            "co2": 0.05,
            "light": 0.05,
        }
        result = analyzer.analyze("spike", attribution, normal_sequence)
        assert result["root_cause"] == "heating_overshoot"
        assert result["confidence"] == 0.70

    def test_spike_co2(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "co2": 0.7,
            "temperature": 0.1,
            "humidity": 0.1,
            "light": 0.1,
        }
        result = analyzer.analyze("spike", attribution, normal_sequence)
        assert result["root_cause"] == "co2_supply_blockage"
        assert result["confidence"] == 0.70


# ---------------------------------------------------------------------------
# RootCauseAnalyzer — saturation
# ---------------------------------------------------------------------------


class TestSaturationPatterns:
    """Saturation pattern with humidity at max."""

    def test_saturation_humidity_at_max(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "humidity": 0.8,
            "temperature": 0.1,
            "co2": 0.05,
            "light": 0.05,
        }
        result = analyzer.analyze("saturation", attribution, normal_sequence)
        assert result["root_cause"] == "humidity_limit_exceeded"
        assert result["confidence"] == 0.80


# ---------------------------------------------------------------------------
# RootCauseAnalyzer — unknown anomaly fallback
# ---------------------------------------------------------------------------


class TestUnknownAnomaly:
    """Pattern type that matches no rule → unknown_anomaly."""

    def test_unrecognized_pattern_falls_back(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.25,
            "humidity": 0.25,
            "co2": 0.25,
            "light": 0.25,
        }
        result = analyzer.analyze(
            "nonexistent_pattern", attribution, normal_sequence
        )
        assert result["root_cause"] == "unknown_anomaly"
        assert result["confidence"] == ROOT_CAUSE_UNKNOWN_CONFIDENCE

    def test_unknown_has_empty_alternatives(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.25,
            "humidity": 0.25,
            "co2": 0.25,
            "light": 0.25,
        }
        result = analyzer.analyze(
            "nonexistent_pattern", attribution, normal_sequence
        )
        assert result["alternative_causes"] == []
        assert result["alternative_confidences"] == []

    def test_unknown_urgency_is_normal(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {"temperature": 0.5, "humidity": 0.5}
        seq = normal_sequence[:, :2]
        custom = RootCauseAnalyzer(sensor_names=["temperature", "humidity"])
        result = custom.analyze("nonexistent", attribution, seq)
        assert result["urgency"] == "normal"


# ---------------------------------------------------------------------------
# RootCauseAnalyzer — input validation
# ---------------------------------------------------------------------------


class TestRootCauseValidation:
    """ValueError on invalid inputs."""

    def test_empty_attribution_raises(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        with pytest.raises(ValueError, match="attribution"):
            analyzer.analyze("drift", {}, normal_sequence)

    def test_empty_pattern_type_raises(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        with pytest.raises(ValueError, match="pattern_type"):
            analyzer.analyze("", {"temperature": 1.0}, normal_sequence)

    def test_whitespace_pattern_type_raises(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        with pytest.raises(ValueError, match="pattern_type"):
            analyzer.analyze("   ", {"temperature": 1.0}, normal_sequence)

    def test_non_finite_sequence_raises(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        seq = np.ones((20, 4))
        seq[5, 2] = np.nan
        with pytest.raises(ValueError, match="non-finite"):
            analyzer.analyze("drift", {"temperature": 1.0}, seq)

    def test_inf_sequence_raises(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        seq = np.ones((20, 4))
        seq[0, 0] = np.inf
        with pytest.raises(ValueError, match="non-finite"):
            analyzer.analyze("drift", {"temperature": 1.0}, seq)

    def test_wrong_shape_sequence_raises(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        with pytest.raises(ValueError, match="2-D"):
            analyzer.analyze(
                "drift", {"temperature": 1.0}, np.ones((20,))
            )

    def test_wrong_sensor_count_raises(
        self, analyzer: RootCauseAnalyzer
    ) -> None:
        with pytest.raises(ValueError, match="sensors"):
            analyzer.analyze(
                "drift", {"temperature": 1.0}, np.ones((20, 3))
            )

    def test_empty_sensor_names_raises(self) -> None:
        with pytest.raises(ValueError, match="sensor_names"):
            RootCauseAnalyzer(sensor_names=[])


# ---------------------------------------------------------------------------
# RootCauseAnalyzer — severity assessment
# ---------------------------------------------------------------------------


class TestSeverityAssessment:
    """Severity classification based on pattern type and sensor count."""

    def test_cascade_is_high_severity(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.4,
            "humidity": 0.4,
            "co2": 0.1,
            "light": 0.1,
        }
        result = analyzer.analyze("cascade", attribution, normal_sequence)
        assert result["severity"] == "high"

    def test_dependency_is_high_severity(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "light": 0.3,
            "temperature": 0.4,
            "humidity": 0.2,
            "co2": 0.1,
        }
        result = analyzer.analyze("dependency", attribution, normal_sequence)
        assert result["severity"] == "high"

    def test_drift_is_medium_severity(
        self, analyzer: RootCauseAnalyzer, constant_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.9,
            "humidity": 0.05,
            "co2": 0.03,
            "light": 0.02,
        }
        result = analyzer.analyze("drift", attribution, constant_sequence)
        assert result["severity"] == "medium"

    def test_spike_single_sensor_is_low_severity(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.9,
            "humidity": 0.05,
            "co2": 0.03,
            "light": 0.02,
        }
        result = analyzer.analyze("spike", attribution, normal_sequence)
        assert result["severity"] == "low"


# ---------------------------------------------------------------------------
# RecommendationEngine — actions for known causes
# ---------------------------------------------------------------------------


class TestRecommendations:
    """Every key in RECOMMENDED_ACTIONS returns actions."""

    def test_all_known_causes_have_actions(
        self, recommender: RecommendationEngine
    ) -> None:
        for cause in RecommendationEngine.RECOMMENDED_ACTIONS:
            result = recommender.recommend(cause, severity="medium")
            assert len(result["all_actions"]) > 0, (
                f"No actions for {cause}"
            )
            assert len(result["immediate_actions"]) > 0

    def test_unknown_cause_gets_default_action(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend("totally_unknown_cause")
        assert len(result["all_actions"]) > 0
        assert "Check system operation" in result["all_actions"][0]


# ---------------------------------------------------------------------------
# RecommendationEngine — severity-based action count limits
# ---------------------------------------------------------------------------


class TestSeverityActionLimits:
    """High → 5, medium → 3, low → 2 actions."""

    def test_high_severity_gets_5_actions(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend(
            "ventilation_failure", severity="high"
        )
        assert len(result["all_actions"]) == RECOMMENDATION_MAX_ACTIONS["high"]

    def test_medium_severity_gets_3_actions(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend(
            "ventilation_failure", severity="medium"
        )
        assert len(result["all_actions"]) == RECOMMENDATION_MAX_ACTIONS[
            "medium"
        ]

    def test_low_severity_gets_2_actions(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend(
            "ventilation_failure", severity="low"
        )
        assert len(result["all_actions"]) == RECOMMENDATION_MAX_ACTIONS["low"]

    def test_immediate_vs_followup_split(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend(
            "ventilation_failure", severity="high"
        )
        assert len(result["immediate_actions"]) == 1
        assert len(result["follow_up_actions"]) == (
            len(result["all_actions"]) - 1
        )


# ---------------------------------------------------------------------------
# RecommendationEngine — technician requirement
# ---------------------------------------------------------------------------


class TestTechnicianRequirement:
    """True for equipment failures, False for sensor issues."""

    def test_compressor_failure_requires_technician(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend("compressor_failure")
        assert result["requires_technician"] is True

    def test_power_supply_requires_technician(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend("power_supply_issue")
        assert result["requires_technician"] is True

    def test_ventilation_failure_requires_technician(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend("ventilation_failure")
        assert result["requires_technician"] is True

    def test_sensor_failure_does_not_require_technician(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend("sensor_failure")
        assert result["requires_technician"] is False

    def test_sensor_malfunction_does_not_require_technician(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend("sensor_malfunction")
        assert result["requires_technician"] is False


# ---------------------------------------------------------------------------
# RecommendationEngine — priority classification
# ---------------------------------------------------------------------------


class TestPriorityClassification:
    """P1_CRITICAL, P2_HIGH, P3_MEDIUM, P4_LOW."""

    def test_p1_critical(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend(
            "compressor_failure", severity="high"
        )
        assert result["priority"] == "P1_CRITICAL"

    def test_p2_high_critical_cause_medium_severity(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend(
            "ventilation_failure", severity="medium"
        )
        assert result["priority"] == "P2_HIGH"

    def test_p2_high_non_critical_cause_high_severity(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend(
            "sensor_failure", severity="high"
        )
        assert result["priority"] == "P2_HIGH"

    def test_p3_medium(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend(
            "sensor_failure", severity="medium"
        )
        assert result["priority"] == "P3_MEDIUM"

    def test_p4_low(
        self, recommender: RecommendationEngine
    ) -> None:
        result = recommender.recommend(
            "sensor_failure", severity="low"
        )
        assert result["priority"] == "P4_LOW"


# ---------------------------------------------------------------------------
# RootCauseAnalyzer — urgency assessment
# ---------------------------------------------------------------------------


class TestUrgencyAssessment:
    """Urgent for compressor/ventilation/power; high for other failures."""

    def test_compressor_failure_is_urgent(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.4,
            "humidity": 0.4,
            "co2": 0.1,
            "light": 0.1,
        }
        result = analyzer.analyze("cascade", attribution, normal_sequence)
        assert result["root_cause"] == "compressor_failure"
        assert result["urgency"] == "urgent"

    def test_ventilation_failure_is_urgent(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "light": 0.3,
            "temperature": 0.4,
            "humidity": 0.2,
            "co2": 0.1,
        }
        result = analyzer.analyze("dependency", attribution, normal_sequence)
        assert result["urgency"] == "urgent"

    def test_sensor_failure_urgency_is_high(
        self, analyzer: RootCauseAnalyzer, constant_sequence: np.ndarray
    ) -> None:
        """'failure' in root_cause but not in URGENT list → 'high'."""
        attribution = {
            "humidity": 0.9,
            "temperature": 0.05,
            "co2": 0.03,
            "light": 0.02,
        }
        result = analyzer.analyze("drift", attribution, constant_sequence)
        assert result["root_cause"] == "humidity_sensor_failure"
        assert result["urgency"] == "high"

    def test_normal_urgency_for_overshoot(
        self, analyzer: RootCauseAnalyzer, normal_sequence: np.ndarray
    ) -> None:
        attribution = {
            "temperature": 0.8,
            "humidity": 0.1,
            "co2": 0.05,
            "light": 0.05,
        }
        result = analyzer.analyze("spike", attribution, normal_sequence)
        assert result["root_cause"] == "heating_overshoot"
        assert result["urgency"] == "normal"


# ---------------------------------------------------------------------------
# RecommendationEngine — downtime estimates
# ---------------------------------------------------------------------------


class TestDowntimeEstimates:
    """Downtime estimates are positive integers for all causes/severities."""

    def test_all_estimates_are_positive_integers(
        self, recommender: RecommendationEngine
    ) -> None:
        for cause in RecommendationEngine.RECOMMENDED_ACTIONS:
            for severity in ("high", "medium", "low"):
                result = recommender.recommend(cause, severity=severity)
                dt = result["estimated_downtime_minutes"]
                assert isinstance(dt, int), f"{cause}/{severity}: not int"
                assert dt > 0, f"{cause}/{severity}: not positive"

    def test_high_severity_increases_downtime(
        self, recommender: RecommendationEngine
    ) -> None:
        med = recommender.recommend(
            "compressor_failure", severity="medium"
        )
        high = recommender.recommend(
            "compressor_failure", severity="high"
        )
        assert high["estimated_downtime_minutes"] > med[
            "estimated_downtime_minutes"
        ]


# ---------------------------------------------------------------------------
# RecommendationEngine — input validation
# ---------------------------------------------------------------------------


class TestRecommendationValidation:
    """ValueError on invalid inputs."""

    def test_empty_root_cause_raises(
        self, recommender: RecommendationEngine
    ) -> None:
        with pytest.raises(ValueError, match="root_cause"):
            recommender.recommend("")

    def test_unknown_severity_raises(
        self, recommender: RecommendationEngine
    ) -> None:
        with pytest.raises(ValueError, match="severity"):
            recommender.recommend("sensor_failure", severity="critical")

    def test_whitespace_root_cause_raises(
        self, recommender: RecommendationEngine
    ) -> None:
        with pytest.raises(ValueError, match="root_cause"):
            recommender.recommend("   ")

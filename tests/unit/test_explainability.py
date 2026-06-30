"""Unit tests for Anomaly model and SHAPExplainer."""

import math
import numpy as np
import pytest

from src.models.anomaly import Anomaly
from src.models.explainability import (
    Explanation,
    FeatureContribution,
    SHAPExplainer,
)


@pytest.fixture
def sample_anomaly() -> Anomaly:
    """Fixture to create a sample Anomaly instance."""
    return Anomaly(
        timestamp="2026-06-23T22:30:27Z",
        sensor="temperature",
        fault_type="spike",
        confidence=0.95,
        physics_valid=True,
        detector_agreement=3,
        season="summer",
        details=[{"raw": 25.5}],
        explanation={"reason": "High temperature spike"},
        cluster_id="c1",
        cluster_severity="high",
    )


@pytest.fixture
def sample_fc() -> FeatureContribution:
    """Fixture to create a sample FeatureContribution."""
    return FeatureContribution("temperature", 0.8, "increase", 1)


@pytest.fixture
def sample_explanation(sample_fc: FeatureContribution) -> Explanation:
    """Fixture to create a sample Explanation."""
    return Explanation("temperature", 0.8, [sample_fc], "Check ventilation")


def test_anomaly_to_dict(sample_anomaly: Anomaly) -> None:
    """Test that the Anomaly dataclass correctly converts to a dictionary."""
    result = sample_anomaly.to_dict()
    assert result["timestamp"] == "2026-06-23T22:30:27Z"
    assert result["sensor"] == "temperature"
    assert result["fault_type"] == "spike"
    assert result["confidence"] == pytest.approx(0.95)
    assert result["physics_valid"] is True
    assert result["detector_agreement"] == 3
    assert result["season"] == "summer"
    assert result["details"] == [{"raw": 25.5}]
    assert result["explanation"] == {"reason": "High temperature spike"}
    assert result["cluster_id"] == "c1"
    assert result["cluster_severity"] == "high"


def test_anomaly_to_dict_with_dataclass_explanation(sample_explanation: Explanation) -> None:
    """Test that Anomaly.to_dict() handles a dataclass explanation object correctly."""
    anomaly = Anomaly(
        timestamp="2026-06-23T22:30:27Z",
        sensor="temperature",
        fault_type="spike",
        confidence=0.95,
        physics_valid=True,
        detector_agreement=3,
        season="summer",
        explanation=sample_explanation,
    )
    result = anomaly.to_dict()
    assert isinstance(result["explanation"], dict)
    assert result["explanation"]["primary_cause"] == "temperature"


def test_anomaly_to_dict_with_dataclass_explanation_no_to_dict() -> None:
    """Test that Anomaly.to_dict() handles a dataclass explanation without to_dict method."""
    from dataclasses import dataclass
    @dataclass
    class DummyExplanation:
        reason: str
    anomaly = Anomaly(
        timestamp="2026-06-23T22:30:27Z",
        sensor="temperature",
        fault_type="spike",
        confidence=0.95,
        physics_valid=True,
        detector_agreement=3,
        season="summer",
        explanation=DummyExplanation("High temperature spike"),
    )
    result = anomaly.to_dict()
    assert isinstance(result["explanation"], dict)
    assert result["explanation"]["reason"] == "High temperature spike"


def test_explanation_and_feature_contribution_to_dict(
    sample_fc: FeatureContribution,
    sample_explanation: Explanation
) -> None:
    """Test that Explanation and FeatureContribution serialize correctly via to_dict."""
    fc_dict = sample_fc.to_dict()
    assert fc_dict == {
        "sensor": "temperature",
        "contribution": pytest.approx(0.8),
        "direction": "increase",
        "importance_rank": 1,
    }
    exp_dict = sample_explanation.to_dict()
    assert exp_dict == {
        "primary_cause": "temperature",
        "confidence": pytest.approx(0.8),
        "contributing_features": [fc_dict],
        "recommendation": "Check ventilation",
    }


def test_shap_explainer_identifies_primary_cause() -> None:
    """Test that SHAPExplainer identifies the primary cause based on detector agreement."""
    pattern = {"statistics": {"temperature": {"mean": 20.0}, "humidity": {"mean": 60.0}}}
    explainer = SHAPExplainer(pattern=pattern)
    anomaly_row = {"temperature": 25.0, "humidity": 55.0}
    detectors = [
        {"sensor": "temperature", "confidence": 0.8, "type": "spike"},
        {"sensor": "humidity", "confidence": 0.5, "type": "drift"},
        {"sensor": "temperature", "confidence": 0.4, "type": "spike"},
    ]
    explanation = explainer.explain(anomaly_row, detectors)
    assert explanation.primary_cause == "temperature"
    assert explanation.confidence == pytest.approx(1.2)
    assert explanation.contributing_features[0].sensor == "temperature"
    assert explanation.contributing_features[0].contribution == pytest.approx(1.2 / 1.7)


def test_shap_explainer_stuck_recommendation() -> None:
    """Test recommendation for a stuck sensor fault."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 20.0}
    detectors = [{"sensor": "temperature", "confidence": 0.9, "type": "stuck"}]
    explanation = explainer.explain(anomaly_row, detectors)
    assert "Sensor failure: temperature sensor may be faulty" in explanation.recommendation


def test_shap_explainer_drift_recommendation() -> None:
    """Test recommendation for a drift sensor fault."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"humidity": 50.0}
    detectors = [{"sensor": "humidity", "confidence": 0.7, "type": "drift"}]
    explanation = explainer.explain(anomaly_row, detectors)
    assert "Calibration drift: humidity sensor needs calibration" in explanation.recommendation


def test_shap_explainer_spike_recommendation() -> None:
    """Test recommendation for a spike sensor fault for mapped and unmapped sensors."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"light": 100.0, "unknown_sensor": 5.0}
    detectors_mapped = [{"sensor": "light", "confidence": 0.8, "type": "spike"}]
    explanation_mapped = explainer.explain(anomaly_row, detectors_mapped)
    assert explanation_mapped.recommendation == "Inspect lighting fixtures and power supply"
    detectors_unmapped = [{"sensor": "unknown_sensor", "confidence": 0.8, "type": "spike"}]
    explanation_unmapped = explainer.explain(anomaly_row, detectors_unmapped)
    assert explanation_unmapped.recommendation == "Investigate anomaly root cause"


def test_shap_explainer_else_recommendation() -> None:
    """Test recommendation for other faults for mapped and unmapped sensors."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"co2": 400.0, "unknown_sensor": 5.0}
    detectors_mapped = [{"sensor": "co2", "confidence": 0.8, "type": "other"}]
    explanation_mapped = explainer.explain(anomaly_row, detectors_mapped)
    assert explanation_mapped.recommendation == "Check CO2 injection system and air exchange"
    detectors_unmapped = [{"sensor": "unknown_sensor", "confidence": 0.8, "type": "other"}]
    explanation_unmapped = explainer.explain(anomaly_row, detectors_unmapped)
    assert explanation_unmapped.recommendation == "Monitor sensor for further anomalies"


def test_shap_explainer_no_detectors() -> None:
    """Test explainer behavior when no detectors are triggered."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0}
    explanation = explainer.explain(anomaly_row, [])
    assert explanation.primary_cause == "Unknown"
    assert explanation.confidence == pytest.approx(0.0)
    assert explanation.recommendation == "Monitor sensor for further anomalies"


def test_shap_explainer_empty_pattern() -> None:
    """Test explainer with empty and missing pattern statistics."""
    explainer_empty = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0}
    detectors = [{"sensor": "temperature", "confidence": 0.8, "type": "spike"}]
    explanation_empty = explainer_empty.explain(anomaly_row, detectors)
    assert explanation_empty.contributing_features[0].direction == "increase"
    explainer_none = SHAPExplainer(pattern={"statistics": None})
    explanation_none = explainer_none.explain(anomaly_row, detectors)
    assert explanation_none.contributing_features[0].direction == "increase"


def test_shap_explainer_directions() -> None:
    """Test the contribution direction (increase vs decrease) relative to mean."""
    pattern = {"statistics": {"temperature": {"mean": 20.0}}}
    explainer = SHAPExplainer(pattern=pattern)
    detectors = [{"sensor": "temperature", "confidence": 0.8, "type": "spike"}]
    explanation_inc = explainer.explain({"temperature": 25.0}, detectors)
    assert explanation_inc.contributing_features[0].direction == "increase"
    explanation_dec = explainer.explain({"temperature": 15.0}, detectors)
    assert explanation_dec.contributing_features[0].direction == "decrease"
    explanation_eq = explainer.explain({"temperature": 20.0}, detectors)
    assert explanation_eq.contributing_features[0].direction == "decrease"


def test_shap_explainer_caps_at_five() -> None:
    """Test that SHAPExplainer caps the contributing features list at top 5."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {f"sensor_{i}": 10.0 for i in range(10)}
    detectors = [{"sensor": f"sensor_{i}", "confidence": 0.5} for i in range(10)]
    explanation = explainer.explain(anomaly_row, detectors)
    assert len(explanation.contributing_features) == 5
    ranks = [feat.importance_rank for feat in explanation.contributing_features]
    assert ranks == [1, 2, 3, 4, 5]


def test_shap_explainer_robust_none_values() -> None:
    """Test SHAPExplainer is robust to None values in confidence and missing statistics."""
    explainer = SHAPExplainer(pattern={"statistics": {"temperature": None}})
    anomaly_row = {"temperature": 25.0}
    detectors = [{"sensor": "temperature", "confidence": None, "type": "spike"}]
    explanation = explainer.explain(anomaly_row, detectors)
    assert explanation.confidence == pytest.approx(0.0)
    assert explanation.contributing_features[0].direction == "increase"


def test_shap_explainer_all_mappings() -> None:
    """Test that recommendations for pressure and power are mapped correctly."""
    explainer = SHAPExplainer(pattern={})
    explanation_press = explainer.explain({"pressure": 1.0}, [{"sensor": "pressure", "confidence": 0.5}])
    assert explanation_press.recommendation == "Inspect sealing and door mechanisms"
    explanation_power = explainer.explain({"power": 1.0}, [{"sensor": "power", "confidence": 0.5}])
    assert explanation_power.recommendation == "Check electrical supply and backup power"


def test_shap_explainer_background_samples() -> None:
    """Test initializing SHAPExplainer with background samples."""
    background = np.array([[1.0, 2.0], [3.0, 4.0]])
    explainer = SHAPExplainer(pattern={}, background_samples=background)
    assert explainer.background_samples is not None
    assert np.array_equal(explainer.background_samples, background)


def test_explainer_invalid_detector_type() -> None:
    """Test behavior when elements in detectors_triggered are not dicts (e.g., None, string, int)."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0}
    with pytest.raises(AttributeError):
        explainer.explain(anomaly_row, [None])  # type: ignore
    with pytest.raises(AttributeError):
        explainer.explain(anomaly_row, ["not-a-dict"])  # type: ignore
    with pytest.raises(AttributeError):
        explainer.explain(anomaly_row, [123])  # type: ignore


def test_explainer_invalid_confidence_values() -> None:
    """Test behavior when confidence in detectors is of invalid types."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0}
    detectors = [{"sensor": "temperature", "confidence": "very-high"}]
    explanation = explainer.explain(anomaly_row, detectors)
    assert explanation.confidence == 0.0


def test_explainer_missing_sensor_key_in_detector() -> None:
    """Test behavior when detectors are missing the 'sensor' key."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0}
    detectors = [{"confidence": 0.8, "type": "spike"}]
    explanation = explainer.explain(anomaly_row, detectors)
    assert explanation.primary_cause == "Unknown"
    assert explanation.confidence == 0.0


def test_explainer_missing_confidence_key_in_detector() -> None:
    """Test behavior when detectors are missing the 'confidence' key."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0}
    detectors = [{"sensor": "temperature", "type": "spike"}]
    explanation = explainer.explain(anomaly_row, detectors)
    assert explanation.primary_cause == "temperature"
    assert explanation.confidence == 0.0


def test_explainer_sensor_not_in_anomaly_row() -> None:
    """Test when detectors trigger on a sensor that is not in the anomaly_row."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0}
    detectors = [{"sensor": "humidity", "confidence": 0.9, "type": "spike"}]
    explanation = explainer.explain(anomaly_row, detectors)
    assert explanation.primary_cause == "humidity"
    assert explanation.confidence == 0.9
    features = {fc.sensor: fc for fc in explanation.contributing_features}
    assert "humidity" in features
    assert features["humidity"].direction == "decrease"


def test_explainer_negative_confidences() -> None:
    """Test explainer with negative detector confidences."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0, "humidity": 60.0}
    detectors = [{"sensor": "temperature", "confidence": -0.5, "type": "spike"}]
    explanation = explainer.explain(anomaly_row, detectors)
    assert explanation.primary_cause == "humidity"
    assert explanation.confidence == 0.0
    features = {fc.sensor: fc for fc in explanation.contributing_features}
    assert features["temperature"].contribution == -0.5


def test_explainer_infinity_and_nan_confidences() -> None:
    """Test explainer behavior with infinity and NaN values in detector confidences."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0}
    explanation_inf = explainer.explain(anomaly_row, [{"sensor": "temperature", "confidence": float("inf")}])
    assert explanation_inf.confidence == 0.0
    assert explanation_inf.contributing_features[0].contribution == 0.0
    explanation_nan = explainer.explain(anomaly_row, [{"sensor": "temperature", "confidence": float("nan")}])
    assert explanation_nan.confidence == 0.0
    assert explanation_nan.contributing_features[0].contribution == 0.0


def test_explainer_performance_very_long_detector_list() -> None:
    """Stress test explainer with 10,000 detectors to measure performance."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0, "humidity": 60.0, "co2": 450.0}
    detectors = [{"sensor": "temperature", "confidence": 0.01, "type": "spike"} for _ in range(10000)]
    import time
    t0 = time.perf_counter()
    explanation = explainer.explain(anomaly_row, detectors)
    duration = time.perf_counter() - t0
    assert explanation.primary_cause == "temperature"
    assert explanation.confidence == pytest.approx(100.0)
    assert duration < 0.1


def test_explainer_extreme_scaling_detectors_and_sensors() -> None:
    """Stress test explainer with 100,000 detectors and 100 sensors."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {f"sensor_{i}": 10.0 for i in range(100)}
    detectors = [{"sensor": f"sensor_{i % 100}", "confidence": 0.001, "type": "drift"} for i in range(100000)]
    import time
    t0 = time.perf_counter()
    explanation = explainer.explain(anomaly_row, detectors)
    duration = time.perf_counter() - t0
    assert len(explanation.contributing_features) == 5
    assert duration < 1.0


def test_anomaly_to_dict_none_values() -> None:
    """Test Anomaly object serialization when fields are set to None or default values."""
    anomaly = Anomaly(
        timestamp="2026-06-23T22:30:27Z",
        sensor="temperature",
        fault_type="spike",
        confidence=0.95,
        physics_valid=True,
        detector_agreement=3,
        season="summer",
        explanation=None,
        cluster_id=None,
        cluster_severity=None,
    )
    result = anomaly.to_dict()
    assert result["explanation"] is None
    assert result["cluster_id"] is None
    assert result["cluster_severity"] is None


def test_anomaly_to_dict_explanation_as_dict() -> None:
    """Test Anomaly serialization when explanation is a dict."""
    exp_dict = {"primary_cause": "temperature", "confidence": 0.9}
    anomaly = Anomaly(
        timestamp="2026-06-23T22:30:27Z",
        sensor="temperature",
        fault_type="spike",
        confidence=0.95,
        physics_valid=True,
        detector_agreement=3,
        season="summer",
        explanation=exp_dict,
    )
    assert anomaly.to_dict()["explanation"] == exp_dict


def test_explainer_none_sensor_value_in_anomaly_row() -> None:
    """Test behavior when anomaly_row has a sensor value of None."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": None}
    detectors = [{"sensor": "temperature", "confidence": 0.8, "type": "spike"}]
    explanation = explainer.explain(anomaly_row, detectors)
    assert explanation.contributing_features[0].direction == "decrease"


def test_explainer_str_sensor_value_in_anomaly_row() -> None:
    """Test behavior when anomaly_row has a sensor value of string."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": "25.0", "humidity": "invalid-float"}
    detectors = [
        {"sensor": "temperature", "confidence": 0.8, "type": "spike"},
        {"sensor": "humidity", "confidence": 0.5, "type": "spike"}
    ]
    explanation = explainer.explain(anomaly_row, detectors)
    feats = {fc.sensor: fc for fc in explanation.contributing_features}
    assert feats["temperature"].direction == "increase"
    assert feats["humidity"].direction == "decrease"


def test_explainer_mixed_sensor_key_types() -> None:
    """Test behavior when detectors have mixed sensor key types (str and int) with equal confidence."""
    explainer = SHAPExplainer(pattern={})
    anomaly_row = {"temperature": 25.0}
    detectors = [
        {"sensor": "temperature", "confidence": 0.8},
        {"sensor": 123, "confidence": 0.8}
    ]
    explanation = explainer.explain(anomaly_row, detectors)
    assert explanation.primary_cause in ("123", "temperature")


def test_shap_explainer_invalid_anomaly_row_type() -> None:
    """Test explainer when anomaly_row is not a dictionary."""
    explainer = SHAPExplainer(pattern={})
    with pytest.raises(TypeError):
        explainer.explain(None, [])  # type: ignore
    with pytest.raises(TypeError):
        explainer.explain([1, 2, 3], [])  # type: ignore


def test_shap_explainer_invalid_detectors_type() -> None:
    """Test explainer when detectors_triggered is not a list."""
    explainer = SHAPExplainer(pattern={})
    with pytest.raises(TypeError):
        explainer.explain({"temperature": 25.0}, None)  # type: ignore
    with pytest.raises(AttributeError):
        explainer.explain({"temperature": 25.0}, ["not a dict"])  # type: ignore


def test_shap_explainer_pattern_none() -> None:
    """Test explainer when pattern is None."""
    explainer = SHAPExplainer(pattern=None)  # type: ignore
    explanation = explainer.explain({"temperature": 25.0}, [{"sensor": "temperature", "confidence": 0.8}])
    assert explanation.primary_cause == "temperature"


def test_shap_explainer_statistics_not_dict() -> None:
    """Test explainer when statistics is not a dict."""
    explainer = SHAPExplainer(pattern={"statistics": "not a dict"})
    explanation = explainer.explain({"temperature": 25.0}, [{"sensor": "temperature", "confidence": 0.8}])
    assert explanation.primary_cause == "temperature"


def test_shap_explainer_sensor_stats_not_dict() -> None:
    """Test explainer when sensor stats is not a dict."""
    explainer = SHAPExplainer(pattern={"statistics": {"temperature": "not a dict"}})
    explanation = explainer.explain({"temperature": 25.0}, [{"sensor": "temperature", "confidence": 0.8}])
    assert explanation.primary_cause == "temperature"


def test_explainer_string_float_confidence() -> None:
    """Test explainer behavior with a string float confidence."""
    explainer = SHAPExplainer(pattern={})
    detectors = [{"sensor": "temperature", "confidence": "0.75"}]
    explanation = explainer.explain({"temperature": 25.0}, detectors)
    assert explanation.confidence == pytest.approx(0.75)


def test_shap_explainer_performance_scale() -> None:
    """Test performance with 2000 sensors and 10000 detectors."""
    anomaly_row = {f"sensor_{i}": 20.0 for i in range(2000)}
    detectors = [{"sensor": f"sensor_{i % 2000}", "confidence": 0.5} for i in range(10000)]
    explainer = SHAPExplainer(pattern={})
    import time
    t0 = time.perf_counter()
    explanation = explainer.explain(anomaly_row, detectors)
    duration = time.perf_counter() - t0
    assert len(explanation.contributing_features) == 5
    assert duration < 0.5


def test_anomaly_to_dict_unserializable_details() -> None:
    """Test Anomaly.to_dict behavior when details contain non-serializable objects."""
    class DummyObj:
        def __str__(self):
            return "Dummy"
    anomaly = Anomaly(
        timestamp="2026-06-23T22:30:27Z",
        sensor="temperature",
        fault_type="spike",
        confidence=0.95,
        physics_valid=True,
        detector_agreement=3,
        season="summer",
        details=[{"obj": DummyObj(), "arr": np.array([1, 2, 3])}],
    )
    result = anomaly.to_dict()
    assert isinstance(result["details"][0]["obj"], DummyObj)
    assert isinstance(result["details"][0]["arr"], np.ndarray)

"""Explainability classes and tools, including SHAPExplainer."""

import math
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
import numpy as np


@dataclass
class FeatureContribution:
    """Represents the contribution of a single feature/sensor to the anomaly."""

    sensor: str
    contribution: float
    direction: str  # 'increase' or 'decrease'
    importance_rank: int

    def to_dict(self) -> Dict:
        """Serializes the feature contribution."""
        return asdict(self)


@dataclass
class Explanation:
    """Represents the explanation details for a detected anomaly."""

    primary_cause: str
    confidence: float
    contributing_features: List[FeatureContribution]
    recommendation: str

    def to_dict(self) -> Dict:
        """Serializes the explanation, mapping contributing features to serialized dicts."""
        return {
            "primary_cause": self.primary_cause,
            "confidence": self.confidence,
            "contributing_features": [fc.to_dict() for fc in self.contributing_features],
            "recommendation": self.recommendation
        }


class SHAPExplainer:
    """Explainer using a simplified SHAP-like approach based on detector agreement."""

    RECOMMENDATIONS = {
        "temperature": "Check ventilation and cooling systems",
        "humidity": "Verify humidification/dehumidification equipment",
        "light": "Inspect lighting fixtures and power supply",
        "co2": "Check CO2 injection system and air exchange",
        "pressure": "Inspect sealing and door mechanisms",
        "power": "Check electrical supply and backup power",
    }

    def __init__(self, pattern: Dict, background_samples: np.ndarray = None):
        """Initialize the SHAPExplainer."""
        self.pattern = pattern
        self.background_samples = background_samples

    def explain(self, anomaly_row: Dict, detectors_triggered: List[Dict]) -> Explanation:
        """Generate an Explanation for the anomaly row."""
        if not isinstance(anomaly_row, dict):
            raise TypeError("anomaly_row must be a dict")
        if not isinstance(detectors_triggered, list):
            raise TypeError("detectors_triggered must be a list")
        clean_row, importances, has_triggered = self._prepare_inputs(anomaly_row, detectors_triggered)
        sorted_sensors = sorted(importances.keys(), key=lambda s: (-importances[s], s))
        denominator = max(sum(importances.values()), 1.0)
        ctx = {"anomaly_row": clean_row, "importances": importances, "denominator": denominator}
        contributions = self._build_contributions(sorted_sensors[:5], ctx)
        cause = sorted_sensors[0] if (has_triggered and sorted_sensors) else "Unknown"
        conf = importances.get(cause, 0.0) if cause != "Unknown" else 0.0
        dets = [d for d in detectors_triggered if d and str(d.get("sensor")) == cause] if cause != "Unknown" else []
        rec = self._get_recommendation(cause, dets)
        return Explanation(cause, conf, contributions, rec)

    def _prepare_inputs(self, anomaly_row: Dict, detectors: List[Dict]) -> tuple:
        """Process inputs and calculate feature importances."""
        clean_row = {str(k): v for k, v in anomaly_row.items()}
        importances = {sensor: 0.0 for sensor in clean_row.keys()}
        has_triggered = False
        for d in (detectors or []):
            if not isinstance(d, dict):
                raise AttributeError("Detector must be a dictionary")
            sensor = d.get("sensor")
            if sensor is not None:
                has_triggered = True
                sensor_str = str(sensor)
                try:
                    conf = float(d.get("confidence"))
                    conf = conf if math.isfinite(conf) else 0.0
                except (TypeError, ValueError):
                    conf = 0.0
                importances[sensor_str] = importances.get(sensor_str, 0.0) + conf
        return clean_row, importances, has_triggered

    def _coerce_val(self, val, base) -> float:
        """Coerce telemetry actual values to float safely."""
        try:
            return float(val) if val is not None else base
        except (ValueError, TypeError):
            return base

    def _build_contributions(self, sensors: List[str], context: Dict) -> List[FeatureContribution]:
        """Build the list of feature contributions ranked by importance."""
        feats = []
        stats = (self.pattern or {}).get("statistics") or {}
        for rank, s in enumerate(sensors, start=1):
            s_stats = stats.get(s) or {} if isinstance(stats, dict) else {}
            base = s_stats.get("mean", 0.0) if isinstance(s_stats, dict) else 0.0
            actual = self._coerce_val(context["anomaly_row"].get(s), base)
            dir_name = "increase" if actual > base else "decrease"
            feats.append(
                FeatureContribution(
                    sensor=s,
                    contribution=float(context["importances"][s] / context["denominator"]),
                    direction=dir_name,
                    importance_rank=rank,
                )
            )
        return feats

    def _get_recommendation(self, primary_sensor: str, detectors: List[Dict]) -> str:
        """Generate a recommendation based on primary sensor and detector type."""
        if not detectors:
            return "Monitor sensor for further anomalies"
        types = [str(d.get("type") or d.get("detector") or d.get("detector_name") or "") for d in detectors]
        if any("stuck" in t for t in types):
            return f"Sensor failure: {primary_sensor} sensor may be faulty. Recalibrate or replace."
        if any("drift" in t for t in types):
            return f"Calibration drift: {primary_sensor} sensor needs calibration. Schedule maintenance."
        return self._get_sensor_recommendation(primary_sensor, types)

    def _get_sensor_recommendation(self, sensor: str, types: List[str]) -> str:
        """Retrieve standard sensor check or fallback recommendation."""
        val = self.RECOMMENDATIONS.get(sensor)
        if any("spike" in t for t in types):
            return val if val is not None else "Investigate anomaly root cause"
        return val if val is not None else "Monitor sensor for further anomalies"

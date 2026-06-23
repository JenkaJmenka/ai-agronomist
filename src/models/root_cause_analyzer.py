"""
Root cause analysis: explains WHY an anomaly occurred.

Combines pattern detection results (what TYPE of anomaly) with sensor
attribution (WHICH sensors caused it) to generate a root cause hypothesis
with supporting evidence.

Rule-based diagnosis engine: ``DIAGNOSIS_RULES`` maps
``(pattern_type, sensors_key)`` combinations to root cause hypotheses with
confidence levels, descriptions, and indicator lists.

Example output of :meth:`RootCauseAnalyzer.analyze`::

    {
        "root_cause": "ventilation_failure",
        "confidence": 0.85,
        "reasoning": "Lights on but temperature dropping...",
        "supporting_evidence": ["Value showed clear declining trend"],
        "alternative_causes": ["humidity_controller_failure"],
        "alternative_confidences": [0.80],
        "severity": "high",
        "urgency": "urgent"
    }
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np

from src.config import (
    RECOMMENDATION_URGENT_CAUSES,
    ROOT_CAUSE_ATTRIBUTION_THRESHOLD,
    ROOT_CAUSE_MULTI_SENSOR_MIN,
    ROOT_CAUSE_UNKNOWN_CONFIDENCE,
    ROOT_CAUSE_VARIANCE_THRESHOLD,
    SENSOR_NAMES,
)
from src.models.diagnosis_rules import (
    DIAGNOSIS_RULES,
    _HIGH_SEVERITY_PATTERNS,
    _INDICATOR_EVIDENCE,
    _MEDIUM_SEVERITY_PATTERNS,
)

logger = logging.getLogger(__name__)


class RootCauseAnalyzer:
    """Generate root cause hypotheses from anomaly patterns and attributions.

    Usage::

        analyzer = RootCauseAnalyzer()
        result = analyzer.analyze(
            pattern_type="dependency",
            attribution={"temperature": 0.4, "light": 0.3, ...},
            sequence=sensor_readings,  # (seq_len, input_dim)
        )

    Pure Python/numpy — no torch model involved.  Consumes the
    ``PatternDetector.detect()['primary_pattern']`` and
    ``AnomalyAttributor.attribute_sequence()['attribution']`` contracts.
    """

    DIAGNOSIS_RULES = DIAGNOSIS_RULES

    def __init__(
        self, sensor_names: Optional[List[str]] = None
    ) -> None:
        """
        Args:
            sensor_names: Per-channel sensor names; defaults to
                ``config.SENSOR_NAMES``.

        Raises:
            ValueError: If ``sensor_names`` is provided but empty.
        """
        self.sensor_names: List[str] = (
            list(sensor_names)
            if sensor_names is not None
            else list(SENSOR_NAMES)
        )
        if not self.sensor_names:
            raise ValueError("sensor_names must contain at least one sensor")

    def analyze(
        self,
        pattern_type: str,
        attribution: Dict[str, float],
        sequence: np.ndarray,
        customer_profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate a root cause hypothesis.

        Args:
            pattern_type: From ``PatternDetector.detect()['primary_pattern']``
                (e.g. ``"drift"``, ``"spike"``, ``"dependency"``).
            attribution: From ``AnomalyAttributor.attribute_sequence()``
                — sensor name → normalized contribution (sums to ~1.0).
            sequence: Sensor readings, shape ``(seq_len, input_dim)``.
            customer_profile: Optional customer-specific context for
                future rule personalization (currently unused).

        Returns:
            Dict with ``root_cause``, ``confidence``, ``reasoning``,
            ``supporting_evidence``, ``alternative_causes``,
            ``alternative_confidences``, ``severity``, ``urgency``.

        Raises:
            ValueError: If ``attribution`` is empty, ``sequence`` has wrong
                shape or non-finite values, or ``pattern_type`` is empty.
        """
        self._validate_inputs(pattern_type, attribution, sequence)

        affected_sensors = [
            sensor
            for sensor, contrib in attribution.items()
            if contrib > ROOT_CAUSE_ATTRIBUTION_THRESHOLD
        ]

        matches = self._match_rules(
            pattern_type, affected_sensors, sequence
        )

        if matches:
            top_match = matches[0]
            return {
                "root_cause": top_match["cause"],
                "confidence": top_match["confidence"],
                "reasoning": top_match["description"],
                "supporting_evidence": self._gather_evidence(
                    sequence, top_match
                ),
                "alternative_causes": [
                    m["cause"] for m in matches[1:3]
                ],
                "alternative_confidences": [
                    m["confidence"] for m in matches[1:3]
                ],
                "severity": self._assess_severity(
                    pattern_type, affected_sensors
                ),
                "urgency": self._assess_urgency(top_match["cause"]),
            }

        return {
            "root_cause": "unknown_anomaly",
            "confidence": ROOT_CAUSE_UNKNOWN_CONFIDENCE,
            "reasoning": "Unable to determine root cause",
            "supporting_evidence": [],
            "alternative_causes": [],
            "alternative_confidences": [],
            "severity": self._assess_severity(
                pattern_type, affected_sensors
            ),
            "urgency": "normal",
        }

    def _validate_inputs(
        self,
        pattern_type: str,
        attribution: Dict[str, float],
        sequence: np.ndarray,
    ) -> None:
        """Reject invalid inputs at the boundary.

        Raises:
            ValueError: On empty pattern_type, empty attribution,
                wrong-shape sequence, or non-finite sequence values.
        """
        if not isinstance(pattern_type, str) or not pattern_type.strip():
            raise ValueError(
                "pattern_type must be a non-empty string, "
                f"got {pattern_type!r}"
            )
        if not attribution:
            raise ValueError(
                "attribution must be a non-empty dict mapping sensor "
                "names to contributions"
            )
        seq = np.asarray(sequence)
        if seq.ndim != 2:
            raise ValueError(
                f"sequence must be 2-D (seq_len, input_dim), "
                f"got shape {seq.shape}"
            )
        if seq.shape[1] != len(self.sensor_names):
            raise ValueError(
                f"sequence has {seq.shape[1]} sensors, expected "
                f"{len(self.sensor_names)} ({self.sensor_names})"
            )
        if not np.isfinite(seq).all():
            raise ValueError(
                "sequence contains non-finite values (NaN/Inf)"
            )

    def _match_rules(
        self,
        pattern_type: str,
        affected_sensors: List[str],
        sequence: np.ndarray,
    ) -> List[Dict[str, Any]]:
        """Match diagnosis rules by pattern type and affected sensors.

        Returns:
            Matching rules sorted by confidence (descending).
        """
        matches: List[Dict[str, Any]] = []

        for (pat, sensors_key), rule in self.DIAGNOSIS_RULES.items():
            if pat != pattern_type:
                continue
            if self._sensors_match(sensors_key, affected_sensors):
                matches.append(rule)

        matches.sort(key=lambda r: r["confidence"], reverse=True)
        return matches

    def _sensors_match(
        self, rule_sensors: str, affected_sensors: List[str]
    ) -> bool:
        """Check whether affected sensors match a rule's sensor key.

        Special cases:

        - ``"multi_sensor"``: matches when more than
          ``ROOT_CAUSE_MULTI_SENSOR_MIN`` sensors are affected.
        - ``"cooling_related"``: matches when both temperature and
          humidity are in the affected set.
        - Standard: any affected sensor name is a substring of the key.
        """
        if rule_sensors == "multi_sensor":
            return len(affected_sensors) > ROOT_CAUSE_MULTI_SENSOR_MIN

        if rule_sensors == "cooling_related":
            lower_sensors = {s.lower() for s in affected_sensors}
            return (
                "temperature" in lower_sensors
                and "humidity" in lower_sensors
            )

        for sensor in affected_sensors:
            if sensor.lower() in rule_sensors.lower():
                return True

        return False

    def _gather_evidence(
        self, sequence: np.ndarray, match: Dict[str, Any]
    ) -> List[str]:
        """Gather supporting evidence strings for a matched rule."""
        evidence: List[str] = []

        for indicator in match.get("indicators", []):
            if indicator in _INDICATOR_EVIDENCE:
                evidence.append(_INDICATOR_EVIDENCE[indicator])
                continue

            else:
                logger.warning(
                    "Indicator %r not in _INDICATOR_EVIDENCE — "
                    "add it to keep evidence strings consistent",
                    indicator,
                )
                evidence.append(indicator.replace("_", " "))

        return evidence

    def _assess_severity(
        self, pattern_type: str, affected_sensors: List[str]
    ) -> str:
        """Assess anomaly severity based on pattern and sensor count.

        Returns:
            ``"high"``, ``"medium"``, or ``"low"``.
        """
        if (
            len(affected_sensors) > ROOT_CAUSE_MULTI_SENSOR_MIN
            or pattern_type in _HIGH_SEVERITY_PATTERNS
        ):
            return "high"
        if pattern_type in _MEDIUM_SEVERITY_PATTERNS:
            return "medium"
        return "low"

    def _assess_urgency(self, root_cause: str) -> str:
        """Assess urgency of required action.

        Returns:
            ``"urgent"``, ``"high"``, or ``"normal"``.
        """
        if root_cause in RECOMMENDATION_URGENT_CAUSES:
            return "urgent"
        if "failure" in root_cause:
            return "high"
        return "normal"

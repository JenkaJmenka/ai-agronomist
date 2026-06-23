"""
Fault type classification from pattern detection output.

Maps pattern_type strings (from PatternDetector) and sensor stats to one
of five canonical SCADA fault categories defined in the research spec.
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class FaultType(str, Enum):
    """Canonical SCADA sensor fault categories."""

    CALIBRATION_DRIFT = "calibration_drift"
    BIAS_OFFSET = "bias_offset"
    STUCK_FAULT = "stuck_fault"
    ACCURACY_DECLINE = "accuracy_decline"
    SPIKE_FAULT = "spike_fault"
    POLLING_DROPOUT = "polling_dropout"
    UNKNOWN = "unknown"


class FaultClassifier:
    """Stateless classifier mapping pattern + sensor context to fault type.

    Rules are evaluated in priority order (first match wins).
    """

    @staticmethod
    def classify(
        pattern_type: str,
        sensor_stats: dict[str, str],
        bias_flag: bool = False,
    ) -> str:
        """Classify a detected anomaly into a fault type string.

        Args:
            pattern_type: Primary pattern from PatternDetector output
                (e.g. ``"drift"``, ``"spike"``, ``"saturation"``).
            sensor_stats: Optional dict with contextual info such as
                ``{"noise_level": "high"}``.
            bias_flag: True when BiasOffsetDetector found a significant
                mean shift in at least one sensor channel.

        Returns:
            One of the ``FaultType`` enum values as a plain string.
        """
        if pattern_type == "polling_dropout":
            return FaultType.POLLING_DROPOUT.value

        if bias_flag:
            return FaultType.BIAS_OFFSET.value

        lowered = pattern_type.lower()

        if "stuck" in lowered or "saturation" in lowered:
            return FaultType.STUCK_FAULT.value

        if "spike" in lowered:
            return FaultType.SPIKE_FAULT.value

        if "drift" in lowered:
            return FaultType.CALIBRATION_DRIFT.value

        noise_level = sensor_stats.get("noise_level", "")
        if noise_level == "high":
            return FaultType.ACCURACY_DECLINE.value

        logger.warning(
            "FaultClassifier: no rule matched pattern_type=%s — returning unknown",
            pattern_type,
        )
        return FaultType.UNKNOWN.value

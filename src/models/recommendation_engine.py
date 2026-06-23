"""
Recommendation engine: from root cause + severity → actionable steps.

Maps root cause identifiers (from
:class:`src.models.root_cause_analyzer.RootCauseAnalyzer`) to prioritized
action lists, technician requirements, and downtime estimates.

All tuning knobs (max actions per severity, urgent-cause list, technician
keywords) live in :mod:`src.config` (``RECOMMENDATION_*``).  Downtime
estimates are business logic and stay in this module.

Example output of :meth:`RecommendationEngine.recommend`::

    {
        "root_cause": "ventilation_failure",
        "immediate_actions": ["Check fan operation..."],
        "follow_up_actions": ["Inspect air ducts...", ...],
        "all_actions": [...],
        "requires_technician": True,
        "estimated_downtime_minutes": 45,
        "priority": "P1_CRITICAL"
    }
"""

import logging
from typing import Any, Dict, List

from src.config import (
    RECOMMENDATION_MAX_ACTIONS,
    RECOMMENDATION_TECH_NOT_REQUIRED_KEYWORDS,
    RECOMMENDATION_TECH_REQUIRED_KEYWORDS,
    RECOMMENDATION_URGENT_CAUSES,
)

logger = logging.getLogger(__name__)

_VALID_SEVERITIES = frozenset({"high", "medium", "low"})


class RecommendationEngine:
    """Generate recommended actions from a root cause diagnosis.

    Usage::

        engine = RecommendationEngine()
        result = engine.recommend("ventilation_failure", severity="high")

    Actions are drawn from ``RECOMMENDED_ACTIONS``; severity controls how
    many are returned via ``config.RECOMMENDATION_MAX_ACTIONS``.
    """

    RECOMMENDED_ACTIONS: Dict[str, List[str]] = {
        "ventilation_failure": [
            "Check fan operation - verify power and control signal",
            "Inspect air ducts for blockage or damage",
            "Check fan motor condition and bearings",
            "Verify fan speed controller is functioning",
            "Reset fan controller if stuck",
        ],
        "heating_efficiency_loss": [
            "Check heating system pressure and gas supply",
            "Verify burner ignition and flame",
            "Check heat exchanger for scale buildup",
            "Verify thermostat calibration",
            "Check heating control valve position",
        ],
        "humidity_controller_failure": [
            "Check misting system water supply and pressure",
            "Verify misting nozzles are clear and functional",
            "Check humidity sensor calibration",
            "Inspect misting solenoid valve",
            "Reset humidity controller",
        ],
        "co2_supply_failure": [
            "Check CO2 cylinder pressure (should be 600-900 PSI)",
            "Verify regulator outlet pressure",
            "Inspect supply tubing for kinks or damage",
            "Check solenoid valve for blockage",
            "Replace CO2 cylinder if pressure too low",
        ],
        "sensor_failure": [
            "Verify sensor is receiving power",
            "Check sensor cable for corrosion or damage",
            "Restart sensor module",
            "Recalibrate sensor with known reference",
            "Replace sensor if failed",
        ],
        "power_supply_issue": [
            "Check main power supply voltage and stability",
            "Verify backup power system (UPS)",
            "Check circuit breaker and fuses",
            "Inspect power cables for damage",
            "Call electrician for power diagnostics",
        ],
        "compressor_failure": [
            "Check compressor oil level and condition",
            "Verify compressor discharge pressure",
            "Listen for unusual compressor noise",
            "Check compressor cooling fan operation",
            "Call HVAC technician for repair",
        ],
        "sensor_malfunction": [
            "Verify sensor connections are secure",
            "Restart sensor module",
            "Check sensor is in correct operating range",
            "Calibrate sensor",
            "Replace sensor if faulty",
        ],
        "heating_overshoot": [
            "Check thermostat setpoint and dead band",
            "Verify heating PID controller tuning",
            "Inspect heating element for short circuit",
            "Reduce heating output capacity",
            "Reset heating controller",
        ],
        "co2_supply_blockage": [
            "Check CO2 supply line for kinks or obstruction",
            "Verify CO2 solenoid valve operation",
            "Inspect regulator for debris",
            "Flush CO2 supply line",
            "Replace CO2 regulator if faulty",
        ],
        "humidity_limit_exceeded": [
            "Check dehumidification system operation",
            "Increase ventilation to reduce humidity",
            "Verify humidity sensor accuracy",
            "Check for water leaks or standing water",
            "Adjust humidity control setpoint",
        ],
        "humidity_sensor_failure": [
            "Verify humidity sensor is receiving power",
            "Check sensor cable for moisture damage",
            "Restart humidity sensor module",
            "Recalibrate with known reference humidity",
            "Replace humidity sensor if failed",
        ],
        "co2_sensor_failure": [
            "Verify CO2 sensor is receiving power",
            "Check sensor cable for damage",
            "Restart CO2 sensor module",
            "Recalibrate with known reference gas",
            "Replace CO2 sensor if failed",
        ],
    }

    # Downtime estimates in minutes per root cause (business logic, not config).
    _DOWNTIME_ESTIMATES: Dict[str, int] = {
        "ventilation_failure": 30,
        "humidity_controller_failure": 20,
        "heating_efficiency_loss": 45,
        "co2_supply_failure": 15,
        "sensor_failure": 10,
        "power_supply_issue": 60,
        "compressor_failure": 120,
        "heating_overshoot": 20,
        "co2_supply_blockage": 15,
        "humidity_limit_exceeded": 25,
        "sensor_malfunction": 10,
        "humidity_sensor_failure": 10,
        "co2_sensor_failure": 10,
    }

    _DEFAULT_DOWNTIME: int = 30
    _HIGH_SEVERITY_MULTIPLIER: float = 1.5

    def __init__(self) -> None:
        """Initialize the recommendation engine."""

    def recommend(
        self,
        root_cause: str,
        severity: str = "medium",
        max_actions: int = 5,
    ) -> Dict[str, Any]:
        """Get recommended actions for a diagnosed root cause.

        Args:
            root_cause: Root cause identifier from
                ``RootCauseAnalyzer.analyze()['root_cause']``.
            severity: ``"high"``, ``"medium"``, or ``"low"``.
            max_actions: Upper bound on total actions returned (overridden
                by severity-based limits from ``RECOMMENDATION_MAX_ACTIONS``).

        Returns:
            Dict with ``root_cause``, ``immediate_actions`` (first action),
            ``follow_up_actions`` (remaining), ``all_actions``,
            ``requires_technician``, ``estimated_downtime_minutes``,
            ``priority``.

        Raises:
            ValueError: If ``root_cause`` is empty or ``severity`` is not
                one of ``high``/``medium``/``low``.
        """
        self._validate_inputs(root_cause, severity)

        actions = list(
            self.RECOMMENDED_ACTIONS.get(
                root_cause, ["Check system operation"]
            )
        )

        severity_max = RECOMMENDATION_MAX_ACTIONS.get(severity, max_actions)
        effective_max = min(severity_max, len(actions))
        recommended = actions[:effective_max]

        return {
            "root_cause": root_cause,
            "immediate_actions": recommended[:1] if recommended else [],
            "follow_up_actions": (
                recommended[1:] if len(recommended) > 1 else []
            ),
            "all_actions": recommended,
            "requires_technician": self._determine_technician_required(
                root_cause
            ),
            "estimated_downtime_minutes": self._estimate_downtime(
                root_cause, severity
            ),
            "priority": self._get_priority(root_cause, severity),
        }

    def _validate_inputs(
        self, root_cause: str, severity: str
    ) -> None:
        """Reject invalid inputs at the boundary.

        Raises:
            ValueError: If root_cause is empty or severity is unknown.
        """
        if not isinstance(root_cause, str) or not root_cause.strip():
            raise ValueError(
                f"root_cause must be a non-empty string, "
                f"got {root_cause!r}"
            )
        if severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"severity must be one of {sorted(_VALID_SEVERITIES)}, "
                f"got {severity!r}"
            )

    def _determine_technician_required(self, root_cause: str) -> bool:
        """Check whether the root cause requires a technician.

        Sensor-level issues (matching
        ``RECOMMENDATION_TECH_NOT_REQUIRED_KEYWORDS``) are excluded even
        when they also contain a "required" keyword (e.g.
        ``"sensor_failure"`` contains ``"failure"`` but not a technician
        issue).
        """
        cause_lower = root_cause.lower()
        if any(
            kw in cause_lower
            for kw in RECOMMENDATION_TECH_NOT_REQUIRED_KEYWORDS
        ):
            return False
        return any(
            kw in cause_lower
            for kw in RECOMMENDATION_TECH_REQUIRED_KEYWORDS
        )

    def _estimate_downtime(
        self, root_cause: str, severity: str
    ) -> int:
        """Estimate minutes until issue is resolved.

        High-severity issues receive a 1.5× multiplier on the base
        estimate.  Always returns a positive integer.
        """
        base = self._DOWNTIME_ESTIMATES.get(
            root_cause, self._DEFAULT_DOWNTIME
        )
        if severity == "high":
            base = int(base * self._HIGH_SEVERITY_MULTIPLIER)
        return max(base, 1)

    def _get_priority(
        self, root_cause: str, severity: str
    ) -> str:
        """Classify action priority.

        Returns:
            ``"P1_CRITICAL"``, ``"P2_HIGH"``, ``"P3_MEDIUM"``, or
            ``"P4_LOW"``.
        """
        is_critical_cause = root_cause in RECOMMENDATION_URGENT_CAUSES
        if is_critical_cause and severity == "high":
            return "P1_CRITICAL"
        if is_critical_cause or severity == "high":
            return "P2_HIGH"
        if severity == "medium":
            return "P3_MEDIUM"
        return "P4_LOW"

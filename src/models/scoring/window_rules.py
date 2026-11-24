"""ADR-014 step A3: setpoint / flatline, band breach and dual-sensor votes.

Copies of pipeline steps 0a+, 0a++ and 0e of
``ComprehensiveInferencePipeline._run_with_components``, with the same
``(window, context, profile, state)`` signature as ``votes.py``. The clock
is ``context.sequence_timestamp``; nothing reads the wall clock.

Band findings are returned as findings only: whether a breach is promoted
to an anomaly (configured overrides, nothing else fired, LSTM quiet) is
precedence, which moves in A4.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional

import numpy as np

from src.models.band_breach_detector import BandBreachDetector
from src.models.bias_dual_sensor_detector import BiasDualSensorDetector
from src.models.correlation_breakdown_detector import CorrelationBreakdownDetector
from src.models.scoring.context import ScoringContext
from src.models.scoring.profile import ScoringProfile
from src.models.scoring.votes import LIGHT_SENSOR, VoteResult, WindowState, window_state
from src.models.setpoint_scada_detector import (
    DEHUMIDIFICATION_FAILURE,
    HUMIDITY_SATURATION_RISK,
    OVER_HEATING,
    SENSOR_FLATLINE,
    THERMAL_DIVERGENCE,
    UNDER_HEATING,
    SetpointAwareScadaDetector,
    climate_context_from_mapping,
)

#: Setpoint finding type -> vote key (``cp._SCADA_TYPE_TO_VOTE``).
SETPOINT_TYPE_TO_VOTE: Mapping[str, str] = MappingProxyType({
    UNDER_HEATING: "under_heating",
    OVER_HEATING: "over_heating",
    DEHUMIDIFICATION_FAILURE: "dehumidification_failure",
    HUMIDITY_SATURATION_RISK: "humidity_saturation_risk",
    THERMAL_DIVERGENCE: "thermal_divergence",
    SENSOR_FLATLINE: "sensor_flatline",
})

BIAS_DUAL_VOTE: str = "bias_dual"
CORRELATION_BREAKDOWN_VOTE: str = "correlation_breakdown"
_UNKNOWN_VARIABLE: str = "unknown"
_MIN_PAIR_SAMPLES: int = 2


def setpoint_votes(
    window: np.ndarray,
    context: ScoringContext,
    profile: ScoringProfile,
    state: Optional[WindowState] = None,
) -> VoteResult:
    """Setpoint, RH, microclimate and 20-sample flatline rules (step 0a+).

    The climate extras reach the rules only when ``profile.climate_context``
    is set (live never passes them). A light-only flatline neither votes
    nor becomes a finding: night light sits at zero for hours.
    """
    del state
    if not profile.legacy_window_rules_enabled:
        return VoteResult()
    climate = context.climate if profile.climate_context else None
    findings = SetpointAwareScadaDetector().detect(
        window,
        context=climate_context_from_mapping(climate),
        timestamp=context.sequence_timestamp,
    )
    votes: Dict[str, List[str]] = {}
    kept: List[Mapping[str, Any]] = []
    for finding in findings:
        is_flatline = finding.anomaly_type == SENSOR_FLATLINE
        affected = [
            s for s in finding.affected_sensors if not (is_flatline and s == LIGHT_SENSOR)
        ]
        if is_flatline and not affected:
            continue
        finding_dict = finding.to_dict()
        if is_flatline:
            finding_dict["affected_sensors"] = affected
        kept.append(finding_dict)
        key = SETPOINT_TYPE_TO_VOTE.get(finding.anomaly_type)
        if key:
            sensors = votes.setdefault(key, [])
            sensors.extend(s for s in affected if s not in sensors)
    return VoteResult(
        votes=MappingProxyType({k: tuple(v) for k, v in votes.items()}),
        findings=tuple(kept),
    )


def band_findings(
    window: np.ndarray,
    context: ScoringContext,
    profile: ScoringProfile,
    state: Optional[WindowState] = None,
) -> VoteResult:
    """Operator day/night crop band breaches of the latest step (step 0a++).

    Reads the effective operational config from the window state. Findings
    only; no vote key (promotion is precedence, A4).
    """
    if not profile.legacy_window_rules_enabled:
        return VoteResult()
    st = state if state is not None else window_state(context)
    findings = BandBreachDetector().detect(
        window, st.operational_config, timestamp=context.sequence_timestamp
    )
    return VoteResult(findings=tuple(f.to_dict() for f in findings))


def dual_sensor_votes(
    window: np.ndarray,
    context: ScoringContext,
    profile: ScoringProfile,
    state: Optional[WindowState] = None,
) -> VoteResult:
    """Dual-sensor bias and correlation breakdown per ``sensor_pairs`` (step 0e).

    Pairs come from ``context.sensor_pairs``; a pair whose series differ in
    length or are shorter than two samples is skipped. A variable is
    appended once per pair that fires, as the pipeline does.
    """
    del window, state
    if not profile.legacy_sensor_pairs_supported or not context.sensor_pairs:
        return VoteResult()
    bias = BiasDualSensorDetector()
    corr = CorrelationBreakdownDetector()
    votes: Dict[str, List[str]] = {}
    for pair in context.sensor_pairs:
        a = np.asarray(pair["sensor_a"], dtype=float)
        b = np.asarray(pair["sensor_b"], dtype=float)
        if len(a) != len(b) or len(a) < _MIN_PAIR_SAMPLES:
            continue
        variable = pair.get("variable", _UNKNOWN_VARIABLE)
        if any(bias.detect(a, b)):
            votes.setdefault(BIAS_DUAL_VOTE, []).append(variable)
        if any(corr.detect(a, b)):
            votes.setdefault(CORRELATION_BREAKDOWN_VOTE, []).append(variable)
    return VoteResult(votes=MappingProxyType({k: tuple(v) for k, v in votes.items()}))

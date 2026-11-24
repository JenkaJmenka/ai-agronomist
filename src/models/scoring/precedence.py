"""ADR-014 step A4b: rule-vote precedence, vote profiles and band promotion.

ADR-014 OQ1(a) (orchestrator decision 2026-11-24) puts the precedence and
label table in the scoring core, owned by ml-engineer, with backend-engineer
as required reviewer of the table. This module is a pure, data-driven copy of
what ``src/api/services/comprehensive_pipeline.py`` (``cp``) holds at
``6c10401`` (after A4a, #154):

* ``RULE_VOTE_TABLE``: one table. It replaces ``cp._RULE_PRECEDENCE``,
  ``cp._RULE_VOTE_PROFILES`` and ``cp._SCADA_VOTE_KEYS``, which are derived
  from it here (``RULE_PRECEDENCE``, ``RULE_VOTE_PROFILES``,
  ``SETPOINT_VOTE_KEYS``). ADR-014 §1 row 20 (vote / precedence), row 21
  (labelling) and row 7 (band breach + promotion).
* ``order_rule_votes``: ``cp._order_rule_votes``.
* ``rule_reasoning`` / ``humanize_sensors``: ``cp.build_rule_reasoning`` /
  ``cp._humanize_sensors``.
* ``apply_rule_votes``: the step 5c decision inside
  ``ComprehensiveInferencePipeline._run_with_components`` (a fired rule
  confirms the anomaly; the highest-precedence rule labels it unless physics
  confirmed a fault).
* ``band_promotion``: the step 5c-last decision (a configured-band breach is
  promoted only when nothing else fired).
* ``primary_rule_is_setpoint``: the step 8 test that lets setpoint prose
  replace the rule reasoning only when a setpoint rule won precedence.

``cp`` keeps its own copies until backend-engineer switches it (next PR);
``tests/models/test_scoring_precedence_a4b.py`` proves the two equal.

No I/O, no wall clock, no mutation of the arguments. Like the rest of the
package it must stay free of ``src.api`` / ``src.db`` / ``src.live`` imports
(ADR-014 §2.1, §3 invariant 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.models.band_breach_detector import BAND_TYPE_TO_VOTE

__all__ = [
    "DEFAULT_RULE_FAULT_TYPE",
    "RULE_PRECEDENCE",
    "RULE_VOTE_PROFILES",
    "RULE_VOTE_TABLE",
    "RuleDecision",
    "RuleVoteRow",
    "SETPOINT_VOTE_KEYS",
    "VoteKind",
    "apply_rule_votes",
    "band_promotion",
    "humanize_sensors",
    "order_rule_votes",
    "primary_rule_is_setpoint",
    "rule_reasoning",
    "validate_rule_table",
]


class VoteKind(str, Enum):
    """Where a vote key sits in the precedence model (ADR-014 §1 row 20).

    ``RULE`` and ``SETPOINT`` rows are ranked: their table order is the
    precedence (first fired key wins). ``SETPOINT`` rows also mark the keys
    whose setpoint prose may replace the rule reasoning (step 8). ``BAND``
    rows are not ranked: a band breach labels a window only through
    ``band_promotion``, after every ranked rule stayed silent (row 7).
    """

    RULE = "rule"
    SETPOINT = "setpoint"
    BAND = "band"


@dataclass(frozen=True)
class RuleVoteRow:
    """One vote key: its precedence class and its fault profile.

    ``reasoning`` names the algorithm that fired; ``{sensors}`` is filled with
    the humanised flagged channels. The other four fields are the labels the
    pipeline reports when this key wins (ADR-014 §1 row 21).
    """

    key: str
    kind: VoteKind
    fault_type: str
    root_cause: str
    severity: str
    urgency: str
    reasoning: str

    def profile(self) -> Mapping[str, str]:
        """The read-only ``{fault_type, root_cause, severity, urgency, reasoning}`` dict."""
        return MappingProxyType({
            "fault_type": self.fault_type,
            "root_cause": self.root_cause,
            "severity": self.severity,
            "urgency": self.urgency,
            "reasoning": self.reasoning,
        })


#: Fault type a fired rule reports when its key has no profile
#: (``cp`` step 5c: ``rule_profile.get("fault_type", "accuracy_decline")``).
DEFAULT_RULE_FAULT_TYPE: str = "accuracy_decline"

_R, _S, _B = VoteKind.RULE, VoteKind.SETPOINT, VoteKind.BAND

#: The single precedence and label table. Ranked rows (RULE, SETPOINT) are in
#: precedence order: the first key that actually voted wins. Short-circuits
#: (dropout, out-of-bounds) return before this table is consulted; band rows
#: apply only when no ranked key fired. Spike is before noise, and BOCPD is
#: before both, so a level shift is not labelled accuracy_decline and a
#: transient is not labelled as precision loss. Setpoint / flatline votes stay
#: after that sensor-fault chain so they cannot steal those labels.
RULE_VOTE_TABLE: Tuple[RuleVoteRow, ...] = (
    # --- ranked sensor-fault rules (ADR-014 §1 rows 8, 9, 11-14, 20) ---
    RuleVoteRow(
        "change_point", _R, "calibration_drift", "level_shift_change_point",
        "medium", "scheduled",
        "Change-point: Bayesian online change-point detection (BOCPD) "
        "flagged a level shift on {sensors}",
    ),
    RuleVoteRow(
        "spike_mad", _R, "spike_fault", "transient_spike", "high", "urgent",
        "Transient spike: {sensors} deviated beyond the rolling "
        "median-absolute-deviation (MAD) threshold",
    ),
    RuleVoteRow(
        "noise", _R, "accuracy_decline", "sensor_noise_floor_exceeded",
        "high", "urgent",
        "Precision degradation: rolling variance on {sensors} exceeded the "
        "calibrated noise-floor ratio",
    ),
    RuleVoteRow(
        "bias_dual", _R, "bias_offset", "redundant_sensor_bias_offset",
        "high", "urgent",
        "Dual-sensor bias: cross-difference between redundant {sensors} "
        "sensors exceeded the calibration tolerance",
    ),
    RuleVoteRow(
        "correlation_breakdown", _R, "correlation_breakdown",
        "spatial_correlation_breakdown", "high", "urgent",
        "Spatial correlation breakdown: rolling Pearson correlation on "
        "{sensors} fell below the expected floor",
    ),
    RuleVoteRow(
        "diurnal", _R, "diurnal_deviation", "diurnal_profile_violation",
        "medium", "scheduled",
        "Diurnal profile deviation: {sensors} exceeded the historical "
        "hourly Z-score for this hour of day",
    ),
    # Mahalanobis is non-voting today (row 14); the row keeps its rank and
    # label so re-arming it is a one-line change in cp.
    RuleVoteRow(
        "multivariate", _R, "accuracy_decline", "multivariate_implausibility",
        "high", "urgent",
        "Multivariate implausibility: the joint {sensors} reading exceeded "
        "the Mahalanobis distance cutoff — each channel is in range but "
        "the combination is inconsistent",
    ),
    # --- ranked setpoint / flatline rules (ADR-014 §1 rows 6, 18, 20) ---
    RuleVoteRow(
        "sensor_flatline", _S, "stuck_fault", "sensor_flatline_frozen_telemetry",
        "high", "urgent",
        "Sensor flatline: {sensors} frozen "
        "(variance ≤ FLATLINE_VARIANCE_EPS²) for the "
        "configured multi-hour window",
    ),
    RuleVoteRow(
        "under_heating", _S, "under_heating", "heating_setpoint_deficit",
        "high", "urgent",
        "Under-heating: {sensors} below climate-computer heating setpoint "
        "by more than the configured margin",
    ),
    RuleVoteRow(
        "over_heating", _S, "over_heating", "cooling_or_ventilation_shortfall",
        "high", "urgent",
        "Over-heating: {sensors} above heating setpoint without solar-driven "
        "outdoor conditions to explain the excess",
    ),
    RuleVoteRow(
        "dehumidification_failure", _S, "dehumidification_failure",
        "dehumidification_capacity_failure", "high", "urgent",
        "Dehumidification failure: {sensors} at critical RH with near-zero "
        "humidity deficit",
    ),
    RuleVoteRow(
        "humidity_saturation_risk", _S, "humidity_saturation_risk",
        "moisture_saturation_near_dewpoint", "medium", "scheduled",
        "Humidity saturation risk: {sensors} at or above the critical RH "
        "threshold near dew point",
    ),
    RuleVoteRow(
        "thermal_divergence", _S, "thermal_divergence", "crop_air_thermal_disconnect",
        "high", "urgent",
        "Thermal divergence: canopy vs greenhouse air on {sensors} exceeds "
        "the microclimate critical delta",
    ),
    # --- unranked band breaches, promoted last (ADR-014 §1 row 7) ---
    RuleVoteRow(
        "temp_band_high", _B, "temp_band_high", "crop_setpoint_band_breach",
        "high", "urgent",
        "Temperature above configured crop day/night max on {sensors}",
    ),
    RuleVoteRow(
        "temp_band_low", _B, "temp_band_low", "crop_setpoint_band_breach",
        "high", "urgent",
        "Temperature below configured crop day/night min on {sensors}",
    ),
    RuleVoteRow(
        "vpd_band_high", _B, "vpd_band_high", "crop_setpoint_band_breach",
        "high", "urgent",
        "VPD above configured crop day/night max (from {sensors})",
    ),
    RuleVoteRow(
        "vpd_band_low", _B, "vpd_band_low", "crop_setpoint_band_breach",
        "medium", "scheduled",
        "VPD below configured crop day/night min (from {sensors})",
    ),
    RuleVoteRow(
        "humidity_band_high", _B, "humidity_band_high", "crop_setpoint_band_breach",
        "high", "urgent",
        "Humidity above configured crop day/night max on {sensors}",
    ),
    RuleVoteRow(
        "humidity_band_low", _B, "humidity_band_low", "crop_setpoint_band_breach",
        "medium", "scheduled",
        "Humidity below configured crop day/night min on {sensors}",
    ),
    RuleVoteRow(
        "co2_band_high", _B, "co2_band_high", "crop_setpoint_band_breach",
        "medium", "scheduled",
        "CO₂ above configured crop day/night max on {sensors}",
    ),
    RuleVoteRow(
        "co2_band_low", _B, "co2_band_low", "crop_setpoint_band_breach",
        "medium", "scheduled",
        "CO₂ below configured crop day/night min on {sensors}",
    ),
)


def validate_rule_table(rows: Iterable[RuleVoteRow]) -> None:
    """Raise ``ValueError`` if the table cannot drive precedence safely.

    The successor of ``cp``'s import-time check (#127: every
    ``_RULE_PRECEDENCE`` key must have a vote profile, so a new precedence
    entry never raises ``KeyError`` in the reasoning or the rule-profile
    lookup). With one table a ranked key always has a row, so the check is
    on the row instead: keys unique, every label field non-empty, ranked rows
    before band rows, and the reasoning template formats with ``sensors``
    alone.
    """
    seen: set[str] = set()
    band_seen = False
    problems: List[str] = []
    for row in rows:
        if not row.key:
            problems.append("empty vote key")
            continue
        if row.key in seen:
            problems.append(f"{row.key!r}: duplicate vote key")
        seen.add(row.key)
        if not isinstance(row.kind, VoteKind):
            problems.append(f"{row.key!r}: kind {row.kind!r} is not a VoteKind")
        empty = [
            name
            for name in ("fault_type", "root_cause", "severity", "urgency", "reasoning")
            if not getattr(row, name)
        ]
        if empty:
            problems.append(f"{row.key!r}: empty profile fields {empty}")
        if row.kind is VoteKind.BAND:
            band_seen = True
        elif band_seen:
            problems.append(f"{row.key!r}: ranked row after a band row")
        try:
            row.reasoning.format(sensors="sensor channels")
        except (KeyError, IndexError, ValueError) as exc:
            problems.append(f"{row.key!r}: reasoning template does not format: {exc!r}")
    if problems:
        raise ValueError("; ".join(problems))


try:
    validate_rule_table(RULE_VOTE_TABLE)
except ValueError as _exc:  # pragma: no cover - fails at import, by design
    raise ImportError(
        "src.models.scoring.precedence: RULE_VOTE_TABLE is inconsistent: "
        f"{_exc} — fix the table row."
    ) from _exc

#: Ranked vote keys in precedence order (``cp._RULE_PRECEDENCE``).
RULE_PRECEDENCE: Tuple[str, ...] = tuple(
    row.key for row in RULE_VOTE_TABLE if row.kind is not VoteKind.BAND
)

#: Vote key -> read-only profile (``cp._RULE_VOTE_PROFILES``).
RULE_VOTE_PROFILES: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {row.key: row.profile() for row in RULE_VOTE_TABLE}
)

#: Setpoint / flatline vote keys (``cp._SCADA_VOTE_KEYS``).
SETPOINT_VOTE_KEYS: frozenset[str] = frozenset(
    row.key for row in RULE_VOTE_TABLE if row.kind is VoteKind.SETPOINT
)

_EMPTY_PROFILE: Mapping[str, str] = MappingProxyType({})


def order_rule_votes(rule_votes: Mapping[str, Sequence[str]]) -> Dict[str, List[str]]:
    """Return the fired votes in ``RULE_PRECEDENCE`` order (first key wins).

    A vote with no sensors has not fired and is dropped. Keys that are not
    ranked keep their relative order and sort after the ranked rules, so a
    new vote is not lost. The sensor lists are copied.
    """
    ordered: Dict[str, List[str]] = {}
    for name in RULE_PRECEDENCE:
        sensors = rule_votes.get(name)
        if sensors:
            ordered[name] = list(sensors)
    for name, sensors in rule_votes.items():
        if name not in ordered and sensors:
            ordered[name] = list(sensors)
    return ordered


def humanize_sensors(sensors: Sequence[str]) -> str:
    """Readable sensor list for reasoning text (``'temperature, humidity'``)."""
    return ", ".join(s.replace("_", " ") for s in sensors) or "sensor channels"


def rule_reasoning(rule_votes: Mapping[str, Sequence[str]]) -> str:
    """Reasoning text that names the algorithm(s) that fired.

    ``rule_votes`` must already be in precedence order (``order_rule_votes``):
    the first key leads with its template, and every co-firing key is appended
    by root cause. An unknown leading key yields generic text, not
    ``KeyError``. Empty when no rule fired (the caller then keeps the model's
    pattern-based reasoning).
    """
    if not rule_votes:
        return ""
    items = list(rule_votes.items())
    primary_name, primary_sensors = items[0]
    primary_profile = RULE_VOTE_PROFILES.get(primary_name)
    if primary_profile is not None:
        template = primary_profile["reasoning"]
    else:
        template = "Anomaly rule '{}' flagged {{sensors}}".format(primary_name)
    reasoning = template.format(sensors=humanize_sensors(primary_sensors))
    if len(items) > 1:
        others = ", ".join(
            RULE_VOTE_PROFILES.get(name, _EMPTY_PROFILE)
            .get("root_cause", name)
            .replace("_", " ")
            for name, _ in items[1:]
        )
        reasoning = f"{reasoning}; also flagged by: {others}"
    return reasoning


@dataclass(frozen=True)
class RuleDecision:
    """Outcome of step 5c (``apply_rule_votes``).

    ``profile`` is the winning rule's profile, ``None`` when no rule fired or
    physics confirmed a fault (physics then keeps its own label). It is an
    empty mapping when the winning key has no profile, exactly as ``cp``.
    """

    is_anomaly: bool
    fault_type: Optional[str]
    profile: Optional[Mapping[str, str]]


def apply_rule_votes(
    rule_votes: Mapping[str, Sequence[str]],
    *,
    is_anomaly: bool,
    fault_type: Optional[str],
    physics_confirmed: bool,
) -> RuleDecision:
    """Step 5c: fired rules confirm the anomaly; the first one labels it.

    ``rule_votes`` must be in precedence order. With no vote the inputs pass
    through unchanged. With votes the window is an anomaly; unless physics
    confirmed a fault, the first key's profile sets ``fault_type``
    (``DEFAULT_RULE_FAULT_TYPE`` for a key with no profile).
    """
    if not rule_votes:
        return RuleDecision(is_anomaly=is_anomaly, fault_type=fault_type, profile=None)
    if physics_confirmed:
        return RuleDecision(is_anomaly=True, fault_type=fault_type, profile=None)
    first_rule = next(iter(rule_votes))
    profile = RULE_VOTE_PROFILES.get(first_rule, _EMPTY_PROFILE)
    return RuleDecision(
        is_anomaly=True,
        fault_type=profile.get("fault_type", DEFAULT_RULE_FAULT_TYPE),
        profile=profile,
    )


def band_promotion(
    band_findings: Sequence[Mapping[str, Any]],
    *,
    overrides_configured: bool,
    physics_suppressed: bool,
    rule_votes: Mapping[str, Sequence[str]],
    is_anomaly: bool,
) -> Optional[Mapping[str, str]]:
    """Step 5c-last: the band profile to promote, or ``None``.

    A configured-band breach is a categorical anomaly, but only as a last
    resort. All must hold: there are (trusted) band findings, the greenhouse
    has stored band overrides (the default profile is not a fault), physics
    did not suppress, no rule fired and the LSTM path did not flag. The first
    finding's ``anomaly_type`` picks the profile through ``BAND_TYPE_TO_VOTE``;
    an unmapped type promotes nothing. On a profile the caller sets
    ``is_anomaly = True``, ``fault_type = profile["fault_type"]`` and uses the
    profile for root cause, severity and urgency.
    """
    if not (
        band_findings
        and overrides_configured
        and not physics_suppressed
        and not rule_votes
        and not is_anomaly
    ):
        return None
    vote_key = BAND_TYPE_TO_VOTE.get(str(band_findings[0].get("anomaly_type", "")))
    profile = RULE_VOTE_PROFILES.get(vote_key or "")
    return profile if profile else None


def primary_rule_is_setpoint(rule_votes: Mapping[str, Sequence[str]]) -> bool:
    """True when the precedence winner is a setpoint / flatline key (step 8).

    Only then may the setpoint finding's prose replace the rule reasoning: a
    co-firing flatline must not replace a spike or change-point explanation.
    """
    return next(iter(rule_votes), None) in SETPOINT_VOTE_KEYS

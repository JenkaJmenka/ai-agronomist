"""ADR-014 step A4b: the core precedence module equals the pipeline's code.

``src/models/scoring/precedence.py`` copies the precedence table, the vote
ordering, the rule reasoning, the step 5c rule decision, the step 5c-last band
promotion and the step 8 setpoint-prose test from
``src/api/services/comprehensive_pipeline.py`` (``cp``) at ``6c10401``.

The references below are that ``cp`` code, held as source text and executed
against the ``cp`` tables frozen in
``tests/fixtures/scoring_precedence_a4b_cp_tables.json``. While ``cp`` still
carries its own copies, ``test_references_are_cps_current_code`` proves the
text and the tables are what ``cp`` runs today. Once backend-engineer switches
``cp`` to the core, ``cp`` stops carrying them and the pin accepts the call
to the core instead; the references stay frozen, so the equivalence tests keep
comparing the core against the pre-switch behaviour rather than against
itself. Change a reference or the fixture only with a measured, intended
behaviour change.

Vote sets compared: every subset of the 13 ranked keys (2**13) in shuffled
insertion order, with unknown, band and empty-sensor votes mixed in; every band
type against every promotion-flag combination; the recorded pre-order votes of
the parity corpus (``tests/fixtures/scoring_core_a3_pipeline_votes.json``, all
three profiles); and seeded random vote sets.
"""

from __future__ import annotations

import ast
import copy
import inspect
import itertools
import json
import random
import textwrap
from pathlib import Path
from types import CodeType, SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from src.api.services import comprehensive_pipeline as cp
from src.config import SENSOR_NAMES
from src.models.band_breach_detector import BAND_TYPE_TO_VOTE
from src.models.scoring import precedence as core

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_TABLES = json.loads((_FIXTURES / "scoring_precedence_a4b_cp_tables.json").read_text(encoding="utf-8"))
_CORPUS_VOTES = json.loads((_FIXTURES / "scoring_core_a3_pipeline_votes.json").read_text(encoding="utf-8"))

_CP_PRECEDENCE: Tuple[str, ...] = tuple(_TABLES["rule_precedence"])
_CP_PROFILES: Dict[str, Dict[str, str]] = {k: dict(v) for k, v in _TABLES["rule_vote_profiles"]}
_CP_SCADA_KEYS: frozenset[str] = frozenset(_TABLES["scada_vote_keys"])

# --- cp's code at 6c10401, verbatim (docstrings and comments dropped) -------

_REF_FUNCTIONS = {
    "_order_rule_votes": '''
def _order_rule_votes(
    rule_votes: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    ordered: Dict[str, List[str]] = {}
    for name in _RULE_PRECEDENCE:
        sensors = rule_votes.get(name)
        if sensors:
            ordered[name] = list(sensors)
    for name, sensors in rule_votes.items():
        if name not in ordered and sensors:
            ordered[name] = list(sensors)
    return ordered
''',
    "_humanize_sensors": '''
def _humanize_sensors(sensors: List[str]) -> str:
    return ", ".join(s.replace("_", " ") for s in sensors) or "sensor channels"
''',
    "build_rule_reasoning": '''
def build_rule_reasoning(rule_votes: Dict[str, List[str]]) -> str:
    if not rule_votes:
        return ""
    items = list(rule_votes.items())
    primary_name, primary_sensors = items[0]
    _primary_profile = _RULE_VOTE_PROFILES.get(primary_name)
    if _primary_profile is not None:
        template = _primary_profile["reasoning"]
    else:
        template = "Anomaly rule '{}' flagged {{sensors}}".format(primary_name)
    reasoning = template.format(sensors=_humanize_sensors(primary_sensors))
    if len(items) > 1:
        others = ", ".join(
            _RULE_VOTE_PROFILES.get(name, {}).get("root_cause", name).replace("_", " ")
            for name, _ in items[1:]
        )
        reasoning = f"{reasoning}; also flagged by: {others}"
    return reasoning
''',
}

# Inline blocks of ComprehensiveInferencePipeline._run_with_components, with
# the core function that replaces each (the pin accepts either).
_REF_BLOCKS = {
    "step_5c_rule_votes": ("apply_rule_votes", '''
rule_profile: Optional[Dict[str, str]] = None
if rule_votes:
    is_anomaly = True
    if not physics_result.confirmed:
        first_rule = next(iter(rule_votes))
        rule_profile = _RULE_VOTE_PROFILES.get(first_rule, {})
        fault_type = rule_profile.get("fault_type", "accuracy_decline")
'''),
    "step_5c_last_band": ("band_promotion", '''
if (
    band_findings
    and _band_overrides_configured
    and not physics_result.suppressed
    and not rule_votes
    and not is_anomaly
):
    _first_band = band_findings[0]
    _band_vote_key = BAND_TYPE_TO_VOTE.get(
        str(_first_band.get("anomaly_type", ""))
    )
    _band_profile = _RULE_VOTE_PROFILES.get(_band_vote_key or "")
    if _band_profile:
        is_anomaly = True
        fault_type = _band_profile["fault_type"]
        rule_profile = _band_profile
'''),
    "step_8_setpoint_prose": ("primary_rule_is_setpoint", '''
primary_rule = next(iter(rule_votes), None)
if scada_anomalies and primary_rule in _SCADA_VOTE_KEYS:
    root_cause_result["reasoning"] = scada_anomalies[0]["reasoning"]
'''),
}


def _ref_namespace() -> Dict[str, Any]:
    ns: Dict[str, Any] = {
        "Any": Any, "Dict": Dict, "List": List, "Optional": Optional,
        "_RULE_PRECEDENCE": _CP_PRECEDENCE,
        "_RULE_VOTE_PROFILES": _CP_PROFILES,
        "_SCADA_VOTE_KEYS": _CP_SCADA_KEYS,
        "BAND_TYPE_TO_VOTE": BAND_TYPE_TO_VOTE,
    }
    for source in _REF_FUNCTIONS.values():
        exec(compile(source, "<cp-reference>", "exec"), ns)
    return ns


_REF = _ref_namespace()
_BLOCK_CODE: Dict[str, CodeType] = {
    name: compile(src, f"<cp-{name}>", "exec") for name, (_, src) in _REF_BLOCKS.items()
}


def _run_block(name: str, **local: Any) -> Dict[str, Any]:
    ns = {**_REF, **local}
    exec(_BLOCK_CODE[name], ns)
    return ns


def _ref_rule_decision(rule_votes: Mapping[str, Sequence[str]], is_anomaly: bool,
                       fault_type: Optional[str], physics_confirmed: bool) -> Tuple[bool, Optional[str], Any]:
    ns = _run_block("step_5c_rule_votes", rule_votes=rule_votes, is_anomaly=is_anomaly,
                    fault_type=fault_type, physics_result=SimpleNamespace(confirmed=physics_confirmed))
    return ns["is_anomaly"], ns["fault_type"], ns["rule_profile"]


def _ref_band(findings: Sequence[Mapping[str, Any]], configured: bool, suppressed: bool,
              rule_votes: Mapping[str, Sequence[str]], is_anomaly: bool) -> Optional[Mapping[str, str]]:
    ns = _run_block("step_5c_last_band", band_findings=findings, _band_overrides_configured=configured,
                    physics_result=SimpleNamespace(suppressed=suppressed), rule_votes=rule_votes,
                    is_anomaly=is_anomaly, fault_type="sentinel", rule_profile=None)
    return ns["rule_profile"]


def _ref_setpoint(rule_votes: Mapping[str, Sequence[str]]) -> bool:
    ns = _run_block("step_8_setpoint_prose", rule_votes=rule_votes,
                    scada_anomalies=[{"reasoning": "setpoint"}], root_cause_result={"reasoning": "rule"})
    return ns["root_cause_result"]["reasoning"] == "setpoint"


def _assert_chain_equal(votes: Dict[str, List[str]]) -> None:
    """Order, reasoning, step 5c and step 8 on one vote set, core vs cp."""
    before = copy.deepcopy(votes)
    ordered = core.order_rule_votes(votes)
    assert list(ordered.items()) == list(_REF["_order_rule_votes"](votes).items()), votes
    assert votes == before and list(votes) == list(before), "order_rule_votes mutated its input"
    for rv in (ordered, votes):
        assert core.rule_reasoning(rv) == _REF["build_rule_reasoning"](rv), rv
    assert core.primary_rule_is_setpoint(ordered) == _ref_setpoint(ordered), ordered
    for is_anomaly, fault_type, physics in itertools.product((False, True), (None, "spike_fault"), (False, True)):
        got = core.apply_rule_votes(ordered, is_anomaly=is_anomaly, fault_type=fault_type,
                                    physics_confirmed=physics)
        want = _ref_rule_decision(ordered, is_anomaly, fault_type, physics)
        assert (got.is_anomaly, got.fault_type) == want[:2], (ordered, is_anomaly, fault_type, physics)
        assert (None if got.profile is None else dict(got.profile)) == want[2], ordered


def _normalised_lines(source: str) -> List[str]:
    out = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


def _contains_run(haystack: List[str], needle: List[str]) -> bool:
    n = len(needle)
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def _function_ast(source: str) -> str:
    node = ast.parse(textwrap.dedent(source)).body[0]
    assert isinstance(node, ast.FunctionDef)
    if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant):
        node.body = node.body[1:]
    return ast.dump(node)


def test_table_is_cps_table_and_validates() -> None:
    assert core.RULE_PRECEDENCE == _CP_PRECEDENCE
    assert {k: dict(v) for k, v in core.RULE_VOTE_PROFILES.items()} == _CP_PROFILES
    assert core.SETPOINT_VOTE_KEYS == _CP_SCADA_KEYS
    # Every precedence key has a profile (cp's #127 import-time check).
    assert all(k in core.RULE_VOTE_PROFILES for k in core.RULE_PRECEDENCE)
    # Every band type the detector emits promotes to a profile.
    assert all(v in core.RULE_VOTE_PROFILES for v in BAND_TYPE_TO_VOTE.values())
    # While cp still owns its tables they must be the frozen ones.
    if cp._RULE_PRECEDENCE is not core.RULE_PRECEDENCE:
        assert tuple(cp._RULE_PRECEDENCE) == _CP_PRECEDENCE
        assert {k: dict(v) for k, v in cp._RULE_VOTE_PROFILES.items()} == _CP_PROFILES
        assert frozenset(cp._SCADA_VOTE_KEYS) == _CP_SCADA_KEYS

    core.validate_rule_table(core.RULE_VOTE_TABLE)
    row = core.RULE_VOTE_TABLE[0]
    band = core.RULE_VOTE_TABLE[-1]
    bad_tables = {
        "duplicate": core.RULE_VOTE_TABLE + (row,),
        "empty profile fields": (core.RuleVoteRow(row.key, row.kind, "", *(
            getattr(row, f) for f in ("root_cause", "severity", "urgency", "reasoning"))),),
        "ranked row after a band row": (band, row),
        "does not format": (core.RuleVoteRow("x", core.VoteKind.RULE, "f", "r", "s", "u", "{sensor} {0}"),),
        "empty vote key": (core.RuleVoteRow("", core.VoteKind.RULE, "f", "r", "s", "u", "{sensors}"),),
    }
    for message, table in bad_tables.items():
        with pytest.raises(ValueError, match=message):
            core.validate_rule_table(table)


def test_references_are_cps_current_code() -> None:
    for name, source in _REF_FUNCTIONS.items():
        fn = getattr(cp, name, None)
        if fn is None or fn.__module__ != cp.__name__:
            continue  # switched: cp re-exports (or no longer has) the core's function
        assert _function_ast(inspect.getsource(fn)) == _function_ast(source), name
    method = inspect.getsource(cp.ComprehensiveInferencePipeline._run_with_components)
    lines = _normalised_lines(method)
    for name, (core_fn, source) in _REF_BLOCKS.items():
        assert _contains_run(lines, _normalised_lines(source)) or f"{core_fn}(" in method, (
            f"{name}: cp neither carries the reference block nor calls {core_fn}"
        )


def test_every_ranked_vote_subset_orders_labels_and_explains_like_cp() -> None:
    rng = random.Random(20261124)
    ranked = list(_CP_PRECEDENCE)
    extras = ["zz_unknown_rule", "temp_band_high", "humidity_band_low"]
    sensors = list(SENSOR_NAMES) + ["pipe_temp_1", "co2_secondary"]
    _assert_chain_equal({})
    for mask in range(1, 1 << len(ranked)):
        keys = [k for i, k in enumerate(ranked) if mask >> i & 1]
        keys += [e for e in extras if rng.random() < 0.3]
        rng.shuffle(keys)
        votes = {k: rng.sample(sensors, rng.randint(1, 3)) for k in keys}
        if rng.random() < 0.2:  # a ranked key that did not fire
            votes[rng.choice(ranked)] = []
        _assert_chain_equal(votes)


def test_band_promotion_and_corpus_vote_sets_match_cp() -> None:
    band_types = list(BAND_TYPE_TO_VOTE) + ["NOT_A_BAND", ""]
    finding_sets: List[List[Dict[str, Any]]] = [[]]
    finding_sets += [[{"anomaly_type": t, "affected_sensors": ["temperature"]}] for t in band_types]
    finding_sets += [[{"affected_sensors": ["humidity"]}]]  # no anomaly_type key
    finding_sets += [[{"anomaly_type": a}, {"anomaly_type": b}] for a, b in itertools.permutations(band_types[:3], 2)]
    rule_vote_sets: List[Dict[str, List[str]]] = [{}, {"noise": ["temperature"]}, {"zz_unknown": ["co2"]}]
    for findings, rv, flags in itertools.product(finding_sets, rule_vote_sets,
                                                 itertools.product((False, True), repeat=3)):
        configured, suppressed, is_anomaly = flags
        got = core.band_promotion(findings, overrides_configured=configured, physics_suppressed=suppressed,
                                  rule_votes=rv, is_anomaly=is_anomaly)
        want = _ref_band(findings, configured, suppressed, rv, is_anomaly)
        assert (None if got is None else dict(got)) == want, (findings, rv, flags)

    # Parity corpus: recorded pre-order votes and band findings, every profile.
    n_cases = 0
    for profile_name, cases in _CORPUS_VOTES.items():
        for case in cases:
            if "votes" not in case:
                continue  # out-of-bounds gate returned before precedence
            votes = {k: list(s) for k, s in case["votes"]}
            _assert_chain_equal(votes)
            ordered = core.order_rule_votes(votes)
            for configured, suppressed in itertools.product((False, True), repeat=2):
                got = core.band_promotion(case["band"], overrides_configured=configured,
                                          physics_suppressed=suppressed, rule_votes=ordered, is_anomaly=False)
                want = _ref_band(case["band"], configured, suppressed, ordered, False)
                assert (None if got is None else dict(got)) == want, (profile_name, case["votes"])
            n_cases += 1
    assert n_cases >= 150, n_cases

    # Seeded random vote sets over every key the table knows plus unknowns.
    rng = random.Random(154)
    pool = list(_CP_PROFILES) + ["zz_a", "zz_b"]
    for _ in range(2000):
        keys = rng.sample(pool, rng.randint(1, 8))
        _assert_chain_equal({k: rng.sample(list(SENSOR_NAMES), rng.randint(0, 2)) for k in keys})

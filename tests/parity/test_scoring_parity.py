"""ADR-014 step A1: golden-window parity across upload, live and the inference API.

The corpus and the per-path runners are in ``_golden_corpus.py``. The harness
runs once per module (about 2-20 s) and every test reads its output.

Three kinds of assertion:

1. **Pairwise parity** (``test_pairwise_parity``). Each golden case is
   compared across path pairs. Where the paths agree today the test asserts
   equality. Where ADR-014 §1 records the difference, the case is
   ``xfail(strict=True, raises=AssertionError)`` and the reason names the
   §1 row. Strict means that a Phase B fix makes the test fail, so the
   marker must then be removed on purpose. ``raises=AssertionError`` means
   a harness crash is never reported as an expected divergence.
2. **Snapshot** (``test_snapshot_reproduced_exactly``). Today's per-path
   records are stored in ``tests/fixtures/scoring_parity_snapshot.json``.
   The serialised output must match the file byte for byte, so a Phase A
   refactor can prove "no behaviour change". To regenerate after an
   intended Phase B change, run this module with
   ``PARITY_SNAPSHOT_UPDATE=1`` and review the diff.
3. **Clock** (``test_wall_clock_is_frozen_and_sequence_time_fixed``). The
   frozen clock reached the code that reads it, so the clock rows show up as
   deterministic divergences and not as flaky ones.

A verdict is ``(is_anomaly, fault_type)``; on the two paths that run the
comprehensive pipeline (live enrichment and ``/comprehensive``) it also
carries the rule votes, because only there do they share one vocabulary.
LSTM scores are rounded to 6 decimals in the records.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from tests.parity._golden_corpus import (
    FROZEN_NOW,
    PATHS,
    SNAPSHOT,
    build_corpus,
    run_harness,
)

CASE_IDS: tuple[str, ...] = (
    "clean_day",
    "clean_night",
    "spike",
    "noise",
    "level_shift",
    "oob",
    "stuck",
    "night_light",
    "heat_excursion",
)

# --------------------------------------------------------------------------
# ADR-014 §1 row ids for every divergence observed on main. Wording is short;
# the row in docs/architecture/ADR-014-one-scoring-path.md is the authority.
# --------------------------------------------------------------------------
_ROW1 = "ADR-014 §1 row 1 (normalisation: upload file-adaptive ranges, live/API fixed SENSOR_RANGES)"
_ROW1_16 = (
    "ADR-014 §1 rows 1 and 16 (upload adaptive ranges absorb the excursion into the file "
    "threshold; upload has no PhysicsValidator temperature rule)"
)
_ROW2_GATE = "ADR-014 §1 row 2 (pipeline OOB gate returns early with anomaly_score sentinel 1.0)"
_ROW5 = "ADR-014 §1 row 5 (LSTM decision: live fixed 0.05, /inference fixed 0.8, /comprehensive max(EWMA,POT))"
_ROW5_OQ6 = (
    "ADR-014 §1 rows 5 and 21, OQ6 (/api/inference is LSTM-only at fixed 0.8 and returns no fault_type)"
)
_ROW8 = "ADR-014 §1 row 8 (noise: upload absent; live gated behind the LSTM, §1 summary item 1)"
_ROW8_UPLOAD = "ADR-014 §1 row 8 (noise vote: absent on upload)"
_ROW8_13 = "ADR-014 §1 rows 8 and 13 (noise / BOCPD window rules: absent on upload)"
_ROW8_13_LIVE = "ADR-014 §1 rows 8 and 13 behind the LSTM on live (§1 summary item 1)"
_ROW9_LIVE = "ADR-014 §1 row 9 behind the LSTM on live (§1 summary item 1: live gates every rule on the LSTM)"
_ROW16_CLOCK = (
    "ADR-014 §1 row 16 (physics nocturnal-light rule reads wall clock on live: no sequence_timestamp)"
)
_ROW16_UPLOAD = "ADR-014 §1 row 16 (upload has no PhysicsValidator nocturnal-light rule)"
_ROW20 = "ADR-014 §1 rows 20 and 21 (two precedence models: upload anomaly_rank_key picks spike_mad over out_of_bounds)"
_ROW21 = "ADR-014 §1 row 21 (/api/inference carries no fault_type; live/comprehensive label from physics)"
_ROW_CLOCK = (
    "ADR-014 §1 rows 12, 16, 26 (live enrichment calls pipeline.run without sequence_timestamp)"
)
_ROW26 = "ADR-014 §1 row 26 (/api/inference high_anomalies[].timestamp = now())"

# comparison id -> {case id -> xfail reason}. Absent case = must agree today.
EXPECTED_DIVERGENCE: dict[str, dict[str, str]] = {
    "score:live=inference": {},
    "score:comprehensive=inference": {"oob": _ROW2_GATE},
    "score:upload=inference": {case: _ROW1 for case in CASE_IDS},
    "verdict:live=comprehensive": {
        "clean_night": _ROW5,
        "spike": _ROW9_LIVE,
        "noise": _ROW8,
        "level_shift": _ROW8_13_LIVE,
        "night_light": _ROW16_CLOCK,
    },
    "verdict:upload=comprehensive": {
        "noise": _ROW8_UPLOAD,
        "level_shift": _ROW8_13,
        "oob": _ROW20,
        "night_light": _ROW16_UPLOAD,
        "heat_excursion": _ROW1_16,
    },
    "verdict:upload=live": {
        "clean_night": _ROW5,
        "spike": _ROW9_LIVE,
        "oob": _ROW20,
        "night_light": _ROW5,
        "heat_excursion": _ROW1_16,
    },
    "verdict:inference=comprehensive": {
        case: _ROW5_OQ6
        for case in ("spike", "noise", "level_shift", "oob", "stuck", "night_light", "heat_excursion")
    },
    "verdict:live=inference": {
        "clean_night": _ROW5,
        "oob": _ROW5,
        "stuck": _ROW5,
        "night_light": _ROW5,
        "heat_excursion": _ROW21,
    },
}

# Clock checks only apply where the path did the clock-reading work.
# Live enrichment runs only when the tick LSTM score crossed the live threshold.
_LIVE_ENRICHED: tuple[str, ...] = ("clean_night", "oob", "stuck", "night_light", "heat_excursion")
# /api/inference emits high_anomalies only above ANOMALY_THRESHOLD.
_INFERENCE_FLAGGED: tuple[str, ...] = ("heat_excursion",)
CLOCK_DIVERGENCE: dict[str, dict[str, str]] = {
    "clock:live_enrichment_gets_sequence_timestamp": {c: _ROW_CLOCK for c in _LIVE_ENRICHED},
    "clock:inference_timestamp_is_sequence_time": {c: _ROW26 for c in _INFERENCE_FLAGGED},
}


def _params() -> list[Any]:
    params = []
    for comparison, divergent in EXPECTED_DIVERGENCE.items():
        for case in CASE_IDS:
            marks = []
            if case in divergent:
                marks = [pytest.mark.xfail(strict=True, raises=AssertionError, reason=divergent[case])]
            params.append(pytest.param(case, comparison, marks=marks, id=f"{case}-{comparison}"))
    for comparison, divergent in CLOCK_DIVERGENCE.items():
        for case, reason in divergent.items():
            marks = [pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)]
            params.append(pytest.param(case, comparison, marks=marks, id=f"{case}-{comparison}"))
    return params


# --------------------------------------------------------------------------
# Harness fixture
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    import logging

    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        return run_harness(tmp_path_factory.mktemp("scoring_parity"))
    finally:
        logging.disable(previous)


def _snapshot_payload(harness: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in harness.items() if not k.startswith("_")}


def _serialise(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def _verdict(record: dict[str, Any], *, with_rules: bool) -> tuple:
    if not with_rules:
        return (record["is_anomaly"], record["fault_type"])
    rules = [d for d in record["detectors"] if d != "lstm"]
    return (record["is_anomaly"], record["fault_type"], rules)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_corpus_is_small_fixed_and_fixture_built() -> None:
    """Nine finite 20x4 windows with fixed timestamps, from committed fixtures."""
    corpus = build_corpus()
    assert tuple(c.case_id for c in corpus) == CASE_IDS
    by_id = {c.case_id: c for c in corpus}
    for case in corpus:
        assert case.window_array.shape == (20, 4), case.case_id
        assert (case.window_array == case.window_array).all(), f"NaN in {case.case_id}"
        gaps = case.frame["timestamp"].diff().dropna().dt.total_seconds().unique().tolist()
        assert gaps == [300.0], f"{case.case_id} cadence {gaps}"
    # Injections touch only the window, never the context rows.
    clean = by_id["clean_day"].frame
    for injected in ("spike", "noise", "level_shift", "oob", "heat_excursion"):
        frame = by_id[injected].frame
        assert frame.iloc[:-20].equals(clean.iloc[:-20]), injected
        assert not frame.iloc[-20:].equals(clean.iloc[-20:]), injected
    assert by_id["clean_night"].window_end.hour == 2
    assert by_id["clean_day"].window_end.hour == 12
    assert set(by_id["stuck"].window["humidity"]) == {100.0}
    assert by_id["oob"].window["temperature"].max() > 80.0


@pytest.mark.parametrize(("case_id", "comparison"), _params())
def test_pairwise_parity(harness: dict[str, Any], case_id: str, comparison: str) -> None:
    """Equal where ADR-014 says the paths agree; strict xfail on a §1 row otherwise."""
    paths = harness["cases"][case_id]["paths"]
    kind, _, pair = comparison.partition(":")
    context = json.dumps(paths, indent=1, sort_keys=True)

    if kind == "score":
        left, right = pair.split("=")
        assert paths[left]["lstm_score"] is not None, context
        assert paths[left]["lstm_score"] == paths[right]["lstm_score"], context
    elif kind == "verdict":
        left, right = pair.split("=")
        with_rules = {left, right} == {"live", "comprehensive"}
        if with_rules:
            live = paths["live"]
            enrichment = live["enrichment"]
            live_view = {
                "is_anomaly": live["is_anomaly"],
                "fault_type": live["fault_type"],
                "detectors": enrichment.get("detectors") or [],
            }
            views = {"live": live_view, "comprehensive": paths["comprehensive"]}
            assert _verdict(views[left], with_rules=True) == _verdict(
                views[right], with_rules=True
            ), context
        else:
            assert _verdict(paths[left], with_rules=False) == _verdict(
                paths[right], with_rules=False
            ), context
    elif comparison == "clock:live_enrichment_gets_sequence_timestamp":
        assert paths["live"]["enrichment"]["ran"], context
        assert paths["live"]["enrichment_got_timestamp"] is True, context
    elif comparison == "clock:inference_timestamp_is_sequence_time":
        ts = paths["inference"]["high_anomaly_timestamp"]
        assert ts is not None, context
        window_end = harness["cases"][case_id]["window_end"]
        assert ts.replace("Z", "+00:00") == window_end, context
    else:  # pragma: no cover - table typo guard
        pytest.fail(f"unknown comparison {comparison}")


def test_snapshot_reproduced_exactly(harness: dict[str, Any]) -> None:
    """Today's per-path records equal the committed snapshot, byte for byte."""
    text = _serialise(_snapshot_payload(harness))
    if os.environ.get("PARITY_SNAPSHOT_UPDATE", "").strip() == "1":
        SNAPSHOT.write_text(text, encoding="utf-8")
        pytest.skip(f"snapshot regenerated at {SNAPSHOT}; review the diff")
    assert SNAPSHOT.is_file(), f"missing {SNAPSHOT}; run with PARITY_SNAPSHOT_UPDATE=1"
    expected = SNAPSHOT.read_text(encoding="utf-8")
    assert text == expected, "per-path scoring output changed (Phase A must be byte-identical)"
    assert set(_snapshot_payload(harness)["cases"]) == set(CASE_IDS)
    for case in CASE_IDS:
        assert set(harness["cases"][case]["paths"]) == set(PATHS)


def test_wall_clock_is_frozen_and_sequence_time_fixed(harness: dict[str, Any]) -> None:
    """The frozen clock reached the code; sequence timestamps are the data's own."""
    assert harness["frozen_now"] == FROZEN_NOW.isoformat()
    # /api/inference stamps the only flagged window with the frozen wall clock.
    ts = harness["cases"]["heat_excursion"]["paths"]["inference"]["high_anomaly_timestamp"]
    assert ts.replace("Z", "+00:00") == FROZEN_NOW.isoformat()
    # The same night window with light on: /comprehensive (timestamp 02:00)
    # confirms nocturnal light; live enrichment (frozen noon) does not.
    night = harness["cases"]["night_light"]["paths"]
    assert harness["cases"]["night_light"]["window_end"].startswith("2026-07-03T02:00")
    assert night["comprehensive"]["physics"] == "nocturnal_light"
    assert night["live"]["enrichment"]["ran"] is True
    assert night["live"]["enrichment"]["physics"] is None


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "NOT IN ADR-014 §1: row 21 says /comprehensive has 'fault_type in response', "
        "but ComprehensiveInferenceResponse has no fault_type field and _to_response drops it"
    ),
)
def test_comprehensive_response_carries_fault_type(harness: dict[str, Any]) -> None:
    """ADR row 21 premise: the /comprehensive HTTP response carries fault_type."""
    flags = {
        case: harness["cases"][case]["paths"]["comprehensive"]["response_has_fault_type"]
        for case in CASE_IDS
    }
    assert all(flags.values()), flags

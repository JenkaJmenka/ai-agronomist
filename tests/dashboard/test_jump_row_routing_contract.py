"""Jump-row routing: stuck rows must never reach the statistical scorer.

Step-5 verification of spec items 16 and 17. The dashboard's jump table is fed
by ``_statistical_anomalies_from_results`` → ``stat_rows_to_jump_rows``, and
the second of those overwrites a missing ``score`` with ``z_score / 5.0``. A
stuck-fault row has no z-score at all, so if one ever reached that scorer its
urgent 1.0 would be replaced by 0.0 — the routing filter is what prevents it,
and the ``None``-guard in the scorer is the defence behind it.

Display formatting for the same rows lives in
``test_stuck_display_contract.py``.
"""

from __future__ import annotations

import pytest

from src.dashboard.components.inference_results import (
    _statistical_anomalies_from_results,
    stat_rows_to_jump_rows,
)
from src.config import STUCK_WALLCLOCK_MIN_HOURS

GATE_HOURS = float(STUCK_WALLCLOCK_MIN_HOURS)
ONSET_TS = "2026-12-18T13:00:00+00:00"
FROZEN_CHANNEL = "humidity"


def _current_stuck_row() -> dict:
    """The shape ``upload_portions._stuck_dict_to_high_detail`` writes today."""
    return {
        "timestamp": ONSET_TS,
        "detected_ts": "2026-12-18T19:00:00+00:00",
        "detector": "stuck_fault",
        "score": 1.0,
        "column": FROZEN_CHANNEL,
        "severity": "urgent",
        "method": "wallclock_stuck",
        "value": 100.0,
        "z_score": None,
        "frozen_hours_at_detection": GATE_HOURS,
    }


def _legacy_stuck_row() -> dict:
    """The pre-branch shape: the duration lives in ``z_score``, no new field."""
    row = _current_stuck_row()
    del row["frozen_hours_at_detection"]
    row["z_score"] = GATE_HOURS
    return row


def _legacy_statistical_row() -> dict:
    """A statistical row also has a numeric ``z_score`` — and always did."""
    return {
        "timestamp": "2026-12-21T13:00:00+00:00",
        "detector": "statistical",
        "score": 0.8,
        "column": "measured_pipe_1_temp",
        "severity": "critical",
        "method": "rolling_z_score",
        "value": 51.0,
        "z_score": GATE_HOURS,
    }


class TestStuckRowsDoNotReachTheJumpRowScorer:
    """Spec 17: ``stat_rows_to_jump_rows`` must never see a stuck row.

    Both branches of ``_statistical_anomalies_from_results`` are exercised:
    the preferred ``statistical_summary.anomalies`` list and the
    ``high_anomalies`` fallback taken when that list is empty. A stuck row
    reaching the scorer would have its urgent 1.0 score overwritten by
    ``z/5.0``.
    """

    def test_summary_path_ignores_a_stuck_row_in_high_anomalies(self) -> None:
        results = {
            "statistical_summary": {"anomalies": [_legacy_statistical_row()]},
            "high_anomalies": [_current_stuck_row(), _legacy_statistical_row()],
        }
        selected = _statistical_anomalies_from_results(results)
        assert [row["detector"] for row in selected] == ["statistical"]

    @pytest.mark.parametrize(
        "stuck_row", [_current_stuck_row(), _legacy_stuck_row()],
        ids=["current-shape", "legacy-shape"],
    )
    def test_fallback_path_excludes_a_stuck_row_of_either_shape(
        self, stuck_row: dict
    ) -> None:
        results = {
            "statistical_summary": {"anomalies": []},
            "high_anomalies": [stuck_row, _legacy_statistical_row()],
        }
        selected = _statistical_anomalies_from_results(results)
        assert [row["column"] for row in selected] == ["measured_pipe_1_temp"]

    def test_the_fallback_path_admits_the_compound_label(self) -> None:
        """Non-vacuity for the filter: it excludes stuck, not everything."""
        compound = dict(_legacy_statistical_row(), detector="lstm+statistical")
        results = {"statistical_summary": {}, "high_anomalies": [compound]}
        assert [row["detector"] for row in _statistical_anomalies_from_results(results)] == [
            "lstm+statistical"
        ]


class TestJumpRowScorerIsNoneSafe:
    """Spec 16: ``inference_results.py:261`` must not raise on ``z_score=None``.

    ``dict.get(key, 0)`` applies its default only when the key is ABSENT, so
    a present-but-null ``z_score`` reached ``float(None)`` before this guard.
    Defence in depth: the routing above means no stuck row should arrive, but
    a leaked one must degrade rather than crash a display path.
    """

    def test_a_none_z_score_does_not_raise(self) -> None:
        rows = stat_rows_to_jump_rows([dict(_current_stuck_row(), score=None)])
        assert len(rows) == 1

    def test_a_none_z_score_scores_zero_rather_than_crashing(self) -> None:
        row = dict(_current_stuck_row())
        del row["score"]
        assert stat_rows_to_jump_rows([row])[0]["score"] == 0.0

    def test_an_absent_z_score_key_also_scores_zero(self) -> None:
        row = dict(_current_stuck_row())
        del row["score"]
        del row["z_score"]
        assert stat_rows_to_jump_rows([row])[0]["score"] == 0.0

    def test_an_existing_score_is_never_overwritten(self) -> None:
        """``setdefault``, not assignment: a leaked stuck row keeps its 1.0."""
        assert stat_rows_to_jump_rows([_current_stuck_row()])[0]["score"] == 1.0

    def test_a_statistical_row_still_gets_its_z_derived_score(self) -> None:
        """A sub-cap z, so the formula itself is observable rather than the cap."""
        row = dict(_legacy_statistical_row(), z_score=3.0)
        del row["score"]
        assert stat_rows_to_jump_rows([row])[0]["score"] == pytest.approx(0.6)

    def test_the_z_derived_score_is_capped_at_one(self) -> None:
        row = dict(_legacy_statistical_row(), z_score=50.0)
        del row["score"]
        assert stat_rows_to_jump_rows([row])[0]["score"] == 1.0

    def test_a_negative_z_score_does_not_raise(self) -> None:
        """The ``/5.0`` formula is out of scope here; not crashing is not."""
        row = dict(_legacy_statistical_row(), z_score=-8.0)
        del row["score"]
        assert isinstance(stat_rows_to_jump_rows([row])[0]["score"], float)

    def test_the_input_rows_are_not_mutated(self) -> None:
        row = _current_stuck_row()
        before = dict(row)
        stat_rows_to_jump_rows([row])
        assert row == before

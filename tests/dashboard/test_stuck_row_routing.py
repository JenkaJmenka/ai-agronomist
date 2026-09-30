"""Stuck rows must never enter the statistical display path.

Mechanism pins for ``inference_results._statistical_anomalies_from_results``
and ``stat_rows_to_jump_rows``. The end-to-end pin over fixture A's real
pipeline output lives in
``tests/api/test_stuck_fault_upload.py::TestStuckRowsNeverReachStatJumpRows``
(that is where the upload harness already exists); this file pins each
individual mechanism in isolation so a regression says *which* barrier fell.

Why it matters: ``stat_rows_to_jump_rows`` derives a canonical score from
``min(1.0, z_score / 5.0)``. Stuck hits carry no z-score at all (their hours
live in ``frozen_hours_at_detection``), so a stuck row reaching that function
gets the None-safe ``0.0`` fallback instead of its real ``score=1.0`` — an
urgent freeze would sink to the bottom of the display cap rather than
crashing, which is exactly the kind of silent failure a pin has to catch.
"""

from __future__ import annotations

from src.dashboard.components.inference_results import (
    _statistical_anomalies_from_results,
    stat_rows_to_jump_rows,
)

# A stuck row in its current shape: no z_score, hours in the new field.
STUCK_ROW: dict = {
    "timestamp": "2026-12-18T13:00:00+00:00",
    "detected_ts": "2026-12-18T19:00:00+00:00",
    "detector": "stuck_fault",
    "column": "humidity",
    "severity": "urgent",
    "score": 1.0,
    "z_score": None,
    "frozen_hours_at_detection": 6.0,
    "value": 100.0,
}

# A legacy stuck row read back from a stored Job.result/JobPortion.result blob,
# persisted before frozen_hours_at_detection existed: its duration is still in
# the numeric z_score field, and there is no backfill migration. Kept because
# discriminating on "does this row have a numeric z_score" would misread it as
# statistical — the detector label is the only safe discriminator.
LEGACY_STUCK_ROW: dict = {
    "timestamp": "2026-12-18T13:00:00+00:00",
    "detector": "stuck_fault",
    "column": "humidity",
    "severity": "urgent",
    "score": 1.0,
    "z_score": 6.0,
}

STATISTICAL_ROW: dict = {
    "timestamp": "2026-12-18T13:05:00+00:00",
    "detector": "statistical",
    "column": "measured_pipe_1_temp",
    "severity": "critical",
    "z_score": 8.5,
    "value": 51.0,
}

COMPOUND_ROW: dict = {
    "timestamp": "2026-12-18T13:10:00+00:00",
    "detector": "lstm+statistical",
    "column": "temperature",
    "severity": "warning",
    "z_score": 4.0,
    "value": 31.0,
}


def _results(high_anomalies: list[dict], stat_anomalies: list[dict]) -> dict:
    return {
        "high_anomalies": high_anomalies,
        "statistical_summary": {
            "anomalies": stat_anomalies,
            "baseline_columns": ["humidity"],
            "total_scored": len(high_anomalies),
        },
    }


class TestStatisticalSummaryPreferred:
    """Barrier 1 — ``statistical_summary.anomalies`` wins when non-empty."""

    def test_stuck_row_in_high_anomalies_is_not_returned(self):
        """A non-empty statistical summary short-circuits the fallback."""
        stat_detail = {
            "timestamp": "2026-12-18T13:05:00+00:00",
            "column": "measured_pipe_1_temp",
            "value": 51.0,
            "z_score": 8.5,
            "severity": "critical",
            "method": "rolling_z_score",
        }
        rows = _statistical_anomalies_from_results(
            _results([STUCK_ROW, STATISTICAL_ROW], [stat_detail])
        )
        assert rows == [stat_detail]

    def test_statistical_detail_rows_carry_no_detector_key(self):
        """``StatisticalAnomalyDetail`` has no ``detector`` field at all.

        This is why barrier 1 is structural rather than a filter: there is no
        detector value on these rows for a stuck label to hide in.
        """
        stat_detail = {
            "timestamp": "2026-12-18T13:05:00+00:00",
            "column": "humidity",
            "value": 100.0,
            "z_score": 8.5,
            "severity": "critical",
            "method": "rolling_z_score",
        }
        rows = _statistical_anomalies_from_results(_results([], [stat_detail]))
        assert "detector" not in rows[0]


class TestHighAnomaliesFallbackFilter:
    """Barrier 2 — the fallback admits statistical-tier rows only."""

    def test_stuck_row_excluded_from_fallback(self):
        rows = _statistical_anomalies_from_results(
            _results([STUCK_ROW, STATISTICAL_ROW], [])
        )
        assert rows == [STATISTICAL_ROW]

    def test_legacy_stuck_row_excluded_from_fallback(self):
        """A numeric ``z_score`` must not make a stuck row look statistical."""
        rows = _statistical_anomalies_from_results(
            _results([LEGACY_STUCK_ROW, STATISTICAL_ROW], [])
        )
        assert rows == [STATISTICAL_ROW]

    def test_compound_detector_label_is_admitted(self):
        """``"lstm+statistical"`` is statistical-tier and must survive.

        The other half of the same pin: the filter has to be permissive
        enough for the compound label, which exact-match filtering on
        ``"statistical"`` has undercounted here before.
        """
        rows = _statistical_anomalies_from_results(
            _results([COMPOUND_ROW, STUCK_ROW], [])
        )
        assert rows == [COMPOUND_ROW]

    def test_solo_lstm_row_excluded_from_fallback(self):
        lstm_row = {
            "timestamp": "2026-12-18T13:15:00+00:00",
            "detector": "lstm",
            "column": None,
            "score": 0.9,
            "z_score": None,
        }
        rows = _statistical_anomalies_from_results(
            _results([lstm_row, STATISTICAL_ROW], [])
        )
        assert rows == [STATISTICAL_ROW]

    def test_statistical_row_without_column_excluded(self):
        """Barrier 3 — a truthy ``column`` is also required."""
        columnless = dict(STATISTICAL_ROW, column=None)
        rows = _statistical_anomalies_from_results(_results([columnless], []))
        assert rows == []


class TestJumpRowScoring:
    """What ``stat_rows_to_jump_rows`` would do to a stuck row if it got one.

    Documents the consequence the barriers above prevent, so the cost of a
    future merge of the two paths is written down and not just implied.
    """

    def test_statistical_row_keeps_its_z_derived_score(self):
        rows = stat_rows_to_jump_rows([dict(STATISTICAL_ROW)])
        assert rows[0]["score"] == 1.0  # min(1.0, 8.5 / 5.0)

    def test_low_z_row_scores_below_one(self):
        rows = stat_rows_to_jump_rows([dict(COMPOUND_ROW)])
        assert rows[0]["score"] == 0.8  # 4.0 / 5.0

    def test_a_leaked_stuck_row_would_lose_its_urgent_score(self):
        """Hypothetical, not reachable today — the cost of a path merge.

        ``score`` is already present on a stuck row, so ``setdefault`` leaves
        the real 1.0 intact here; the damage shows on any stuck row reaching
        this function *without* a score, which is the shape the statistical
        summary path produces.
        """
        scoreless_stuck = {
            key: value for key, value in STUCK_ROW.items() if key != "score"
        }
        rows = stat_rows_to_jump_rows([scoreless_stuck])
        assert rows[0]["score"] == 0.0, (
            "A stuck row has no z_score, so the statistical score formula "
            "can only produce 0.0 for it — an urgent freeze scored as the "
            "least important row on the page"
        )
        assert rows[0]["frozen_hours_at_detection"] == 6.0

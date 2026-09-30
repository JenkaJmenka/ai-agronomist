"""``trim_cold_run_rows``: which rows survive a row-count cap, and in what order.

Step-5 verification of spec item 18. The cap used to sort on the first of
``score``/``z_score``/``anomaly_score`` it found, which meant an urgent
stuck-fault freeze — a row with no ``z_score`` at all — competed on score
alone against statistical warnings, and any group of rows sharing that one
number kept whatever order it arrived in. It now sorts on
``anomaly_rank_key``.

Two halves, matching the spec: the real fixture A rows (where the urgent rows
must survive) and a synthetic lstm-heavy list (where arrival order is
deliberately the inverse of rank, so a cap that kept list order would keep
exactly the wrong rows and cannot pass by accident).
"""

from __future__ import annotations

import random

import pytest

from src.api.services.anomaly_ranking import anomaly_rank_key
from src.dashboard.services.anomalies_page_data import trim_cold_run_rows
from tests.api.test_fixture_a_stuck_contract import (
    REFERENCE_CHUNK_SIZE,
    fixture_a_response,
)

CAP = 5
LSTM_ROW_COUNT = 20
SHUFFLE_ROUNDS = 20
SHUFFLE_SEED = 5_18_20260930

EXPECTED_STUCK_ONSET_COUNT = 4


def _row_identity(row: dict) -> tuple:
    return (row.get("timestamp"), row.get("column"), row.get("detector"))


# ---------------------------------------------------------------------------
# 18a — urgent stuck rows survive the cap on fixture A
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fixture_a_rows() -> list[dict]:
    """Fixture A's ``high_anomalies``, serialized as the stored blob holds them."""
    response = fixture_a_response(REFERENCE_CHUNK_SIZE)
    return [hit.model_dump(mode="json") for hit in response.high_anomalies]


class TestUrgentRowsSurviveTheCapOnFixtureA:
    """The regression that motivated the change, checked on real rows."""

    def test_the_input_is_actually_larger_than_the_cap(self, fixture_a_rows) -> None:
        """Non-vacuity: below the cap ``trim_cold_run_rows`` returns its input."""
        assert len(fixture_a_rows) > CAP

    def test_the_input_contains_the_four_urgent_rows(self, fixture_a_rows) -> None:
        urgent = [row for row in fixture_a_rows if row.get("severity") == "urgent"]
        assert len(urgent) == EXPECTED_STUCK_ONSET_COUNT

    def test_every_urgent_row_survives_a_cap_the_size_of_their_count(
        self, fixture_a_rows
    ) -> None:
        """The tightest cap that can still hold them all: nothing else may."""
        survivors = trim_cold_run_rows(
            fixture_a_rows, limit=EXPECTED_STUCK_ONSET_COUNT
        )
        assert [row["severity"] for row in survivors] == (
            ["urgent"] * EXPECTED_STUCK_ONSET_COUNT
        )
        assert {row["detector"] for row in survivors} == {"stuck_fault"}

    def test_the_urgent_rows_lead_the_cap_at_every_limit(self, fixture_a_rows) -> None:
        for limit in (CAP, 10, 40):
            survivors = trim_cold_run_rows(fixture_a_rows, limit=limit)
            leading = survivors[:EXPECTED_STUCK_ONSET_COUNT]
            assert [row["severity"] for row in leading] == (
                ["urgent"] * EXPECTED_STUCK_ONSET_COUNT
            ), f"limit={limit}: urgent rows did not lead, got {survivors[:6]}"

    def test_the_urgent_rows_have_no_z_score_to_compete_on(
        self, fixture_a_rows
    ) -> None:
        """Why the old score/|z| cap could drop them: there is no z to read."""
        urgent = [row for row in fixture_a_rows if row.get("severity") == "urgent"]
        assert [row["z_score"] for row in urgent] == [None] * len(urgent)

    def test_survivors_are_stable_across_shuffled_input(self, fixture_a_rows) -> None:
        """Chunk-size invariance, stated as the property it actually is.

        The cap must depend only on the rows, not on the order they arrive in
        — which is what varies when an upload's chunk boundary moves.
        """
        baseline = [_row_identity(row) for row in trim_cold_run_rows(fixture_a_rows)]
        rng = random.Random(SHUFFLE_SEED)
        for _ in range(SHUFFLE_ROUNDS):
            shuffled = list(fixture_a_rows)
            rng.shuffle(shuffled)
            assert [
                _row_identity(row) for row in trim_cold_run_rows(shuffled)
            ] == baseline

    def test_an_uncapped_list_is_returned_unchanged(self, fixture_a_rows) -> None:
        """Below the limit the function short-circuits — order is untouched."""
        head = fixture_a_rows[:CAP]
        assert trim_cold_run_rows(head, limit=CAP) == head


# ---------------------------------------------------------------------------
# 18b — on an lstm-heavy list, the survivors are the top-k by score
# ---------------------------------------------------------------------------

class TestLstmHeavyCapKeepsTheHighestScores:
    """Solo-lstm rows tie on severity, detector and |z|, so ``score`` decides.

    Scores ascend with arrival index, so rank order is the exact inverse of
    list order: a cap that returned the first k rows would return the k
    LOWEST scores. That inversion is what makes this test non-vacuous.
    """

    @staticmethod
    def _lstm_rows() -> list[dict]:
        return [
            {
                "detector": "lstm",
                "severity": None,
                "column": None,
                "z_score": None,
                "score": round(0.04 * (index + 1), 4),
                "timestamp": f"2026-06-01T{index:02d}:00:00+00:00",
            }
            for index in range(LSTM_ROW_COUNT)
        ]

    def test_survivors_are_the_highest_scores(self) -> None:
        rows = self._lstm_rows()
        survivors = trim_cold_run_rows(rows, limit=CAP)
        expected = sorted((row["score"] for row in rows), reverse=True)[:CAP]
        assert [row["score"] for row in survivors] == expected

    def test_survivors_are_not_the_first_k_rows(self) -> None:
        """The guard that makes the assertion above mean something."""
        rows = self._lstm_rows()
        survivors = trim_cold_run_rows(rows, limit=CAP)
        assert [row["score"] for row in survivors] != [
            row["score"] for row in rows[:CAP]
        ]

    def test_every_lstm_row_ties_on_the_first_three_key_levels(self) -> None:
        """Why ``score`` is load-bearing here and not a cosmetic tiebreak."""
        assert {anomaly_rank_key(row)[:3] for row in self._lstm_rows()} == {(0, 1, 0.0)}

    def test_one_urgent_row_outranks_every_lstm_score(self) -> None:
        """Severity is level 1: a maximal lstm score cannot overturn it."""
        rows = self._lstm_rows()
        rows.append(
            {
                "detector": "stuck_fault",
                "severity": "urgent",
                "column": "humidity",
                "z_score": None,
                "frozen_hours_at_detection": 6.0,
                "score": 0.01,
                "timestamp": "2026-06-01T00:00:00+00:00",
            }
        )
        survivors = trim_cold_run_rows(rows, limit=CAP)
        assert survivors[0]["detector"] == "stuck_fault"
        assert max(row["score"] for row in rows if row["detector"] == "lstm") > 0.01

    def test_rows_with_no_detector_or_score_do_not_raise(self) -> None:
        """``StatisticalAnomalyDetail`` dicts carry neither field.

        They reach the cap through ``statistical_summary.anomalies``, so the
        key must degrade to the identity levels rather than raising mid-sort.
        """
        stat_details = [
            {
                "timestamp": f"2026-12-18T13:{minute:02d}:00+00:00",
                "column": "measured_pipe_1_temp",
                "value": 51.0,
                "z_score": 8.5,
                "severity": "critical",
                "method": "rolling_z_score",
            }
            for minute in range(10)
        ]
        assert len(trim_cold_run_rows(stat_details, limit=CAP)) == CAP

    def test_a_none_timestamp_does_not_raise_mid_sort(self) -> None:
        """Comparing ``None`` against ``str`` would raise ``TypeError``."""
        rows = self._lstm_rows()
        rows[0] = dict(rows[0], timestamp=None)
        assert len(trim_cold_run_rows(rows, limit=CAP)) == CAP

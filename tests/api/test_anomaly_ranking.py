"""``anomaly_rank_key`` — the shared severity/detector ranking.

Used at two sites: trigger attribution among hits sharing a timestamp
(``cold_run_upload.build_trigger_data_by_timestamp``) and the display-cap sort
key that decides which rows survive a row-count limit
(``anomalies_page_data.trim_cold_run_rows``). Both reduce to
``sorted(rows, key=anomaly_rank_key, reverse=True)``, which is what these
tests apply directly.

The key is a 6-tuple: severity > detector tier > |z| > score > timestamp >
column. Levels 4-6 exist because the levels above them tie in real data —
every solo-lstm hit shares ``severity=None``/``detector="lstm"``/
``z_score=None``, so without ``score`` the cap degenerated to arrival order
for any lstm-heavy upload. These tests pin each level that can decide an
outcome on its own.
"""

from __future__ import annotations

import random

import pytest

from src.api.services.anomaly_ranking import anomaly_rank_key, is_statistical_detector

DISPLAY_CAP = 5
SHUFFLE_ROUNDS = 20
SHUFFLE_SEED = 20260930


def _ranked(rows: list[dict], *, limit: int | None = None) -> list[dict]:
    """The exact ranking both production call sites apply."""
    ranked = sorted(rows, key=anomaly_rank_key, reverse=True)
    return ranked if limit is None else ranked[:limit]


def _lstm_row(score: float, index: int) -> dict:
    """A solo-lstm hit: no severity, no column, no z-score — only a score."""
    return {
        "detector": "lstm",
        "severity": None,
        "column": None,
        "z_score": None,
        "score": score,
        "timestamp": f"2026-06-01T{index:02d}:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# score breaks ties within a tier
# ---------------------------------------------------------------------------

class TestScoreDecidesAmongLstmRows:
    """Solo-lstm rows tie on every earlier level, so ``score`` must decide."""

    LSTM_ROW_COUNT = 20

    def _rows(self) -> list[dict]:
        # Scores ascending with index so that arrival order is the *inverse*
        # of rank — a ranking that degenerated to list order would keep the
        # worst rows, not the best, and could not pass by accident.
        return [
            _lstm_row(score=round(0.05 * (i + 1), 4), index=i)
            for i in range(self.LSTM_ROW_COUNT)
        ]

    def test_top_k_by_score_survive_the_cap(self):
        kept = _ranked(self._rows(), limit=DISPLAY_CAP)
        expected = sorted(
            (row["score"] for row in self._rows()), reverse=True
        )[:DISPLAY_CAP]
        assert [row["score"] for row in kept] == expected

    def test_cap_does_not_keep_arrival_order(self):
        """Non-vacuity guard: arrival order and rank order must differ here."""
        rows = self._rows()
        kept = _ranked(rows, limit=DISPLAY_CAP)
        assert [row["score"] for row in kept] != [
            row["score"] for row in rows[:DISPLAY_CAP]
        ]

    def test_every_lstm_row_shares_the_first_three_key_levels(self):
        """Why ``score`` is load-bearing rather than a cosmetic tiebreak."""
        prefixes = {anomaly_rank_key(row)[:3] for row in self._rows()}
        assert prefixes == {(0, 1, 0.0)}


# ---------------------------------------------------------------------------
# severity outranks score
# ---------------------------------------------------------------------------

class TestSeverityOutranksScore:
    """Severity is level 1; no score can overturn it."""

    URGENT_LOW_SCORE = {
        "detector": "stuck_fault",
        "severity": "urgent",
        "column": "humidity",
        "z_score": None,
        "frozen_hours_at_detection": 6.0,
        "score": 0.20,
        "timestamp": "2026-06-01T00:00:00+00:00",
    }
    CRITICAL_HIGH_SCORE = {
        "detector": "statistical",
        "severity": "critical",
        "column": "temperature",
        "z_score": 9.9,
        "score": 0.99,
        "timestamp": "2026-06-01T01:00:00+00:00",
    }

    def test_urgent_ranks_ahead_of_critical(self):
        ranked = _ranked([self.CRITICAL_HIGH_SCORE, self.URGENT_LOW_SCORE])
        assert ranked[0]["severity"] == "urgent"

    def test_urgent_wins_despite_a_lower_score_and_no_z_score(self):
        """The stuck row loses levels 3 and 4 outright and still ranks first."""
        urgent_key = anomaly_rank_key(self.URGENT_LOW_SCORE)
        critical_key = anomaly_rank_key(self.CRITICAL_HIGH_SCORE)
        assert urgent_key[2:4] < critical_key[2:4]
        assert urgent_key > critical_key

    def test_compound_detector_ranks_in_the_statistical_tier(self):
        """``"lstm+statistical"`` must not fall through to the unknown tier.

        Detector tier is matched by membership in the ``"+"``-joined label;
        exact string equality has undercounted the compound label before.
        """
        compound = dict(self.CRITICAL_HIGH_SCORE, detector="lstm+statistical")
        assert anomaly_rank_key(compound)[1] == anomaly_rank_key(
            self.CRITICAL_HIGH_SCORE
        )[1]


# ---------------------------------------------------------------------------
# shuffle determinism
# ---------------------------------------------------------------------------

class TestOrderIsIndependentOfInputOrder:
    """Rows tied through ``score`` are still ordered by their own identity.

    Levels 5 and 6 (``timestamp``, ``column``) are derived from the row, not
    from where it landed in a list, so the result cannot vary with chunk size
    or arrival order. Shuffling the input is the cheapest way to pin that.
    """

    @staticmethod
    def _identity(row: dict) -> tuple:
        return (row["timestamp"], row["column"])

    def _assert_shuffle_invariant(self, rows: list[dict]) -> list[dict]:
        baseline = _ranked(rows)
        rng = random.Random(SHUFFLE_SEED)
        for _ in range(SHUFFLE_ROUNDS):
            shuffled = list(rows)
            rng.shuffle(shuffled)
            assert [self._identity(r) for r in _ranked(shuffled)] == [
                self._identity(r) for r in baseline
            ]
        return baseline

    def test_rows_tied_through_score_order_by_descending_timestamp(self):
        """Tied on severity/detector/|z|/score; only timestamps differ."""
        rows = [
            {
                "detector": "stuck_fault",
                "severity": "urgent",
                "column": "humidity",
                "z_score": None,
                "score": 1.0,
                "timestamp": ts,
            }
            for ts in (
                "2026-12-14T00:00:00+00:00",
                "2026-12-15T11:05:00+00:00",
                "2026-12-16T11:20:00+00:00",
                "2026-12-18T13:00:00+00:00",
            )
        ]
        baseline = self._assert_shuffle_invariant(rows)
        assert [r["timestamp"] for r in baseline] == sorted(
            (r["timestamp"] for r in rows), reverse=True
        )

    def test_rows_tied_through_timestamp_order_by_descending_column(self):
        """Tied through level 5 too; ``column`` is the last-resort key.

        This is the case the trigger-attribution site depends on: every
        candidate in a group shares one timestamp by construction, so
        ``column`` does all the remaining work there.
        """
        shared_ts = "2026-12-18T13:00:00+00:00"
        rows = [
            {
                "detector": "statistical",
                "severity": "critical",
                "column": column,
                "z_score": 7.0,
                "score": 0.9,
                "timestamp": shared_ts,
            }
            for column in ("humidity", "measured_hd", "temperature")
        ]
        baseline = self._assert_shuffle_invariant(rows)
        assert [r["column"] for r in baseline] == [
            "temperature", "measured_hd", "humidity",
        ]

    def test_rows_identical_on_all_six_levels_produce_one_identical_key(self):
        """The documented remnant tie, stated as what it actually is.

        Two rows agreeing on all six key fields are indistinguishable *to the
        key*, so their relative order follows the stable sort's input order —
        it is not recovered from anything on the row. Shuffle-invariance of
        the key sequence holds; shuffle-invariance of any field the key does
        not read (``value`` here) does not, and pinning it would be pinning
        ``sorted``'s stability rather than this function's behaviour.
        """
        base = {
            "detector": "lstm",
            "severity": None,
            "column": None,
            "z_score": None,
            "score": 0.5,
            "timestamp": "2026-06-01T00:00:00+00:00",
        }
        first, second = dict(base, value=1.0), dict(base, value=2.0)
        assert anomaly_rank_key(first) == anomaly_rank_key(second)
        assert [r["value"] for r in _ranked([first, second])] == [1.0, 2.0]
        assert [r["value"] for r in _ranked([second, first])] == [2.0, 1.0]


# ---------------------------------------------------------------------------
# shape tolerance
# ---------------------------------------------------------------------------

class TestKeyToleratesBothRowShapes:
    """The two call sites hand in model instances and raw dicts alike."""

    def test_statistical_detail_dict_without_score_or_detector(self):
        """``StatisticalAnomalyDetail`` dicts have neither field.

        They must fall through to the identity levels rather than raising.
        """
        stat_detail = {
            "timestamp": "2026-12-18T13:05:00+00:00",
            "column": "measured_pipe_1_temp",
            "value": 51.0,
            "z_score": 8.5,
            "severity": "critical",
            "method": "rolling_z_score",
        }
        key = anomaly_rank_key(stat_detail)
        assert key[1] == 0  # no detector key at all → unknown tier
        assert key[3] == 0.0  # no score key at all → 0.0 default

    def test_pydantic_instance_ranks_the_same_as_its_dump(self):
        from src.api.schemas.sensor_data import UploadHighAnomalyDetail

        hit = UploadHighAnomalyDetail(
            score=1.0,
            timestamp="2026-12-18T13:00:00+00:00",
            detector="stuck_fault",
            column="humidity",
            severity="urgent",
            z_score=None,
            frozen_hours_at_detection=6.0,
        )
        assert anomaly_rank_key(hit) == anomaly_rank_key(hit.model_dump())

    @pytest.mark.parametrize(
        "missing_identity", [{"timestamp": None}, {"column": None}],
    )
    def test_none_identity_fields_do_not_raise_mid_sort(self, missing_identity):
        """``None`` vs ``str`` comparison would raise ``TypeError`` mid-sort."""
        rows = [
            dict(_lstm_row(score=0.5, index=1), **missing_identity),
            _lstm_row(score=0.5, index=2),
        ]
        assert len(_ranked(rows)) == 2


# ---------------------------------------------------------------------------
# Task 2c item 3 — is_statistical_detector classifies by membership, not
# exact equality, so a compound label never undercounts.
# ---------------------------------------------------------------------------

class TestIsStatisticalDetectorClassifiesByMembership:
    """Every detector string the backend actually emits (grepped from
    src/api/, src/models/, src/workers/): "lstm", "statistical",
    "lstm+statistical", "stuck_fault", and the "unknown" degrade-path
    default.
    """

    @pytest.mark.parametrize(
        "detector,expected",
        [
            ("statistical", True),
            ("lstm+statistical", True),
            ("lstm", False),
            ("stuck_fault", False),
            ("unknown", False),
            ("", False),
            (None, False),
        ],
    )
    def test_every_backend_detector_string_classified_correctly(
        self, detector, expected
    ):
        assert is_statistical_detector(detector) is expected

    def test_a_future_second_compound_label_is_not_undercounted(self):
        """The exact-match bug this replaces only worked because
        "lstm+statistical" is the sole compound label in the codebase today.
        A membership check must not regress if a second one appears.
        """
        assert is_statistical_detector("statistical+stuck_fault") is True

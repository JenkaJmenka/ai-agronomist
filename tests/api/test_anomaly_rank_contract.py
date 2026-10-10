"""Trigger attribution through the production call site, not the bare key.

Step-5 verification. ``tests/api/test_anomaly_ranking.py`` pins
``anomaly_rank_key`` in isolation; this file pins the same ordering contract
as observed through ``cold_run_upload.build_trigger_data_by_timestamp`` — the
site that actually decides which detector gets credit for a timestamp and
lands in ``SensorReading.trigger_channel`` / ``trigger_detector``. A key that
ranked correctly while the caller compared it wrongly would pass there and
fail here.

Every case puts its candidates on ONE timestamp, because that is the only
situation the caller arbitrates: it groups by normalised timestamp and keeps
the single best hit per group.
"""

from __future__ import annotations

import random

import pytest

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.api.services.anomaly_ranking import anomaly_rank_key
from src.api.services.cold_run_upload import build_trigger_data_by_timestamp

SHARED_TS = "2026-12-18T13:00:00+00:00"
SHUFFLE_ROUNDS = 20
SHUFFLE_SEED = 5_20260930

# High enough that a |z|-only tiebreak would pick the statistical row over an
# urgent freeze by a wide margin — the exact regression this ranking replaced.
DOMINATING_Z = 50.0


def _hit(**overrides: object) -> UploadHighAnomalyDetail:
    fields: dict = {"score": 0.5, "timestamp": SHARED_TS, "detector": "lstm"}
    fields.update(overrides)
    return UploadHighAnomalyDetail(**fields)


def _stuck(**overrides: object) -> UploadHighAnomalyDetail:
    return _hit(
        detector="stuck_fault",
        column="humidity",
        severity="urgent",
        method="wallclock_stuck",
        z_score=None,
        frozen_hours_at_detection=6.0,
        value=100.0,
        score=1.0,
        **overrides,
    )


def _statistical(*, z_score: float, severity: str, column: str) -> UploadHighAnomalyDetail:
    return _hit(
        detector="statistical",
        column=column,
        severity=severity,
        method="rolling_z_score",
        z_score=z_score,
        value=42.0,
    )


def _lstm(*, score: float, value: float) -> UploadHighAnomalyDetail:
    return _hit(detector="lstm", column=None, severity=None, z_score=None,
                score=score, value=value)


def _winner(hits: list[UploadHighAnomalyDetail]) -> dict:
    """The trigger the production site attributes to the shared timestamp."""
    triggers = build_trigger_data_by_timestamp(hits)
    assert set(triggers) == {SHARED_TS}, (
        f"Expected one trigger group at {SHARED_TS}, got {sorted(triggers)}"
    )
    return triggers[SHARED_TS]


def _winner_both_orders(hits: list[UploadHighAnomalyDetail]) -> dict:
    """Same winner whichever candidate the caller sees first.

    The caller keeps its running best on a strict ``>``, so feeding the
    eventual loser first is what would expose an inverted comparison.
    """
    forward = _winner(hits)
    backward = _winner(list(reversed(hits)))
    assert forward == backward, (
        f"Winner depends on input order: {forward} vs {backward}"
    )
    return forward


# ---------------------------------------------------------------------------
# 1. severity beats an overwhelming z-score
# ---------------------------------------------------------------------------

def test_stuck_fault_wins_over_a_statistical_hit_with_fifty_sigma() -> None:
    """Spec 1: urgent freeze + |z| = 50 on another column → freeze triggers.

    The stuck hit has no z_score at all, so under the pre-branch |z|-only
    tiebreak it scored 0.0 and lost this comparison outright.
    """
    stuck = _stuck()
    loud_statistical = _statistical(
        z_score=DOMINATING_Z, severity="critical", column="measured_pipe_1_temp"
    )
    trigger = _winner_both_orders([stuck, loud_statistical])

    assert trigger["detector"] == "stuck_fault"
    assert trigger["channel"] == "humidity"
    assert trigger["severity"] == "urgent"
    # Non-vacuity: the loser really would have won on |z|.
    assert anomaly_rank_key(stuck)[2] < anomaly_rank_key(loud_statistical)[2]


# ---------------------------------------------------------------------------
# 2. within the statistical tier, |z| decides
# ---------------------------------------------------------------------------

def test_higher_absolute_z_wins_between_two_statistical_hits() -> None:
    """Spec 2: two statistical hits on one timestamp → higher |z| triggers."""
    weaker = _statistical(z_score=4.0, severity="warning", column="measured_hd")
    stronger = _statistical(z_score=9.0, severity="warning", column="co2")
    trigger = _winner_both_orders([weaker, stronger])

    assert trigger["channel"] == "co2"


def test_negative_z_is_compared_by_magnitude() -> None:
    """A downward excursion is as severe as an upward one of the same size."""
    downward = _statistical(z_score=-9.0, severity="warning", column="co2")
    upward = _statistical(z_score=4.0, severity="warning", column="measured_hd")

    assert _winner_both_orders([upward, downward])["channel"] == "co2"


# ---------------------------------------------------------------------------
# 3-4. statistical outranks solo lstm at both severities
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("severity", ["critical", "warning"])
def test_statistical_outranks_solo_lstm_regardless_of_severity(severity: str) -> None:
    """Spec 3 and 4: solo lstm (severity None) never takes the timestamp.

    Parametrised over both statistical severities: pinning only ``critical``
    would leave the ``warning`` case, where the level-1 margin is one
    severity step rather than two, untested.
    """
    statistical = _statistical(z_score=3.5, severity=severity, column="measured_hd")
    # Score 1.0 — the maximum — so the lstm row loses despite topping level 4.
    lstm = _lstm(score=1.0, value=7.0)
    trigger = _winner_both_orders([statistical, lstm])

    assert trigger["detector"] == "statistical"
    assert trigger["channel"] == "measured_hd"
    assert trigger["severity"] == severity
    assert anomaly_rank_key(lstm)[3] > anomaly_rank_key(statistical)[3]


# ---------------------------------------------------------------------------
# 5. the compound label ranks in the statistical tier
# ---------------------------------------------------------------------------

class TestCompoundDetectorRanksWithStatistical:
    """``"lstm+statistical"`` must be matched by membership, never equality.

    Exact-match filtering on this label has undercounted statistical hits in
    this repo before, so the compound label is checked from both sides: it
    must beat solo lstm (so it is not falling through to the unknown tier)
    and it must compete with plain ``"statistical"`` on |z| (so it is in that
    tier rather than one of its own).
    """

    COMPOUND_COLUMN = "measured_plant_temp"

    def _compound(self, z_score: float) -> UploadHighAnomalyDetail:
        return _hit(
            detector="lstm+statistical",
            column=self.COMPOUND_COLUMN,
            severity="warning",
            method="rolling_z_score",
            z_score=z_score,
            value=31.0,
            agreement=True,
        )

    def test_compound_beats_solo_lstm(self) -> None:
        compound = self._compound(z_score=3.0)
        trigger = _winner_both_orders([compound, _lstm(score=1.0, value=7.0)])

        assert trigger["detector"] == "lstm+statistical"
        assert trigger["channel"] == self.COMPOUND_COLUMN

    def test_compound_loses_to_a_higher_z_statistical_hit(self) -> None:
        """Same tier, so level 3 decides — not the label."""
        compound = self._compound(z_score=3.0)
        louder = _statistical(z_score=8.0, severity="warning", column="co2")

        assert _winner_both_orders([compound, louder])["channel"] == "co2"

    def test_compound_beats_a_lower_z_statistical_hit(self) -> None:
        """The mirror case: the compound label is no handicap either."""
        compound = self._compound(z_score=8.0)
        quieter = _statistical(z_score=3.0, severity="warning", column="co2")

        assert _winner_both_orders([compound, quieter])["channel"] == self.COMPOUND_COLUMN

    def test_compound_is_not_in_the_unknown_tier(self) -> None:
        plain = _statistical(z_score=3.0, severity="warning", column="co2")
        assert anomaly_rank_key(self._compound(3.0))[1] == anomaly_rank_key(plain)[1]


# ---------------------------------------------------------------------------
# 6. score decides between two solo lstm hits
# ---------------------------------------------------------------------------

def test_higher_score_wins_between_two_lstm_hits() -> None:
    """Spec 6: two lstm rows on one timestamp → higher score triggers.

    Both rows carry ``severity=None``, ``detector="lstm"``, ``column=None``
    and ``z_score=None``, so levels 1-3 and levels 5-6 all tie and ``score``
    is the only field that can decide. ``value`` identifies the winner
    because ``channel`` is ``None`` for both.
    """
    quieter = _lstm(score=0.55, value=1.0)
    louder = _lstm(score=0.95, value=2.0)
    trigger = _winner_both_orders([quieter, louder])

    assert trigger["detector"] == "lstm"
    assert trigger["channel"] is None
    assert trigger["value"] == 2.0
    assert anomaly_rank_key(quieter)[:3] == anomaly_rank_key(louder)[:3]
    assert anomaly_rank_key(quieter)[4:] == anomaly_rank_key(louder)[4:]


# ---------------------------------------------------------------------------
# 7. ties through level 4 are broken by the documented identity keys
# ---------------------------------------------------------------------------

class TestTieThroughScoreFallsToTheIdentityKeys:
    """Rows tied on severity, detector, |z| and score, differing on identity.

    Levels 5 and 6 (``timestamp``, ``column``) are read off the row itself,
    so the outcome is fixed by the data and cannot vary with arrival order or
    with where an upload chunk boundary fell. That is what makes shuffling a
    valid check HERE — unlike rows identical on all six levels, whose keys
    are equal and whose relative order is only ``sorted``'s stability (see
    ``test_anomaly_ranking.py::
    test_rows_identical_on_all_six_levels_produce_one_identical_key``).
    """

    COLUMNS = ("co2", "humidity", "measured_hd", "temperature")
    TIMESTAMPS = ("2026-12-14T00:00:00+00:00", "2026-12-18T13:00:00+00:00")

    def _rows(self) -> list[dict]:
        return [
            {
                "detector": "statistical",
                "severity": "critical",
                "z_score": 7.0,
                "score": 0.9,
                "timestamp": timestamp,
                "column": column,
            }
            for timestamp in self.TIMESTAMPS
            for column in self.COLUMNS
        ]

    @staticmethod
    def _order(rows: list[dict]) -> list[tuple[str, str]]:
        ranked = sorted(rows, key=anomaly_rank_key, reverse=True)
        return [(row["timestamp"], row["column"]) for row in ranked]

    def test_all_rows_tie_through_level_four(self) -> None:
        """Non-vacuity: without levels 5-6 every key here would be identical."""
        assert {anomaly_rank_key(row)[:4] for row in self._rows()} == {
            (2, 2, 7.0, 0.9)
        }

    def test_order_is_descending_timestamp_then_descending_column(self) -> None:
        expected = sorted(
            ((timestamp, column) for timestamp in self.TIMESTAMPS
             for column in self.COLUMNS),
            reverse=True,
        )
        assert self._order(self._rows()) == expected

    def test_order_is_identical_across_repeated_runs(self) -> None:
        rows = self._rows()
        assert len({tuple(self._order(rows)) for _ in range(SHUFFLE_ROUNDS)}) == 1

    def test_order_is_identical_across_shuffled_input(self) -> None:
        rows = self._rows()
        baseline = self._order(rows)
        rng = random.Random(SHUFFLE_SEED)
        for _ in range(SHUFFLE_ROUNDS):
            shuffled = list(rows)
            rng.shuffle(shuffled)
            assert self._order(shuffled) == baseline

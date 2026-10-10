"""A mixed ``JobPortion.result`` blob keeps both ``z_score`` fields distinct.

Step-5 verification of spec item 15. One blob holds two row types that both
have a field named ``z_score`` meaning different things:

* ``stat_hits`` are ``StatisticalAnomalyDetail`` — ``z_score`` is a required
  float and is a z-score.
* ``stuck_hits`` are ``UploadHighAnomalyDetail`` — ``z_score`` is ``None`` and
  the duration lives in ``frozen_hours_at_detection``. Rows persisted before
  that field existed still carry the duration in ``z_score`` instead, and
  there is no backfill migration, so both shapes come back out of storage.

The blob is round-tripped through ``json.dumps``/``json.loads`` because the
column is a JSON column: anything that survives in memory but not through
JSON (a ``NaN``, a ``datetime``) would pass a same-process comparison and
fail in production.
"""

from __future__ import annotations

import json

import pytest

from src.api.schemas.sensor_data import (
    StatisticalAnomalyDetail,
    UploadHighAnomalyDetail,
)
from src.api.services.upload_portions import PortionScores
from src.config import STUCK_WALLCLOCK_MIN_HOURS

GATE_HOURS = float(STUCK_WALLCLOCK_MIN_HOURS)
STAT_Z_SCORE = 8.5
ONSET_TS = "2026-12-18T13:00:00+00:00"
STAT_TS = "2026-12-21T13:00:00+00:00"


def _current_stuck_hit() -> UploadHighAnomalyDetail:
    return UploadHighAnomalyDetail(
        score=1.0,
        timestamp=ONSET_TS,
        detected_ts="2026-12-18T19:00:00+00:00",
        detector="stuck_fault",
        column="humidity",
        severity="urgent",
        method="wallclock_stuck",
        value=100.0,
        z_score=None,
        frozen_hours_at_detection=GATE_HOURS,
    )


def _legacy_stuck_hit() -> UploadHighAnomalyDetail:
    """No ``frozen_hours_at_detection`` key at all — the duration is in z_score."""
    return UploadHighAnomalyDetail(
        score=1.0,
        timestamp=ONSET_TS,
        detector="stuck_fault",
        column="humidity",
        severity="urgent",
        method="wallclock_stuck",
        value=100.0,
        z_score=GATE_HOURS,
    )


def _stat_hit() -> StatisticalAnomalyDetail:
    return StatisticalAnomalyDetail(
        timestamp=STAT_TS,
        column="measured_pipe_1_temp",
        value=51.0,
        z_score=STAT_Z_SCORE,
        severity="critical",
        method="rolling_z_score",
    )


def _blob(portion: PortionScores) -> dict:
    """The ``stat_hits``/``stuck_hits`` halves of the stored portion payload.

    Mirrors ``upload_consumer._portion_payload``: ``model_dump()`` per hit,
    then through JSON as the column stores it.
    """
    payload = {
        "stat_hits": [hit.model_dump() for hit in portion.stat_hits],
        "stuck_hits": [hit.model_dump() for hit in portion.stuck_hits],
    }
    return json.loads(json.dumps(payload))


def _rehydrate(blob: dict) -> PortionScores:
    """Mirrors ``upload_consumer.handle_upload_finalize``'s reconstruction."""
    return PortionScores(
        stat_hits=[StatisticalAnomalyDetail(**row) for row in blob["stat_hits"]],
        stuck_hits=[UploadHighAnomalyDetail(**row) for row in blob["stuck_hits"]],
    )


@pytest.fixture
def mixed_portion() -> PortionScores:
    return PortionScores(
        stat_hits=[_stat_hit()],
        stuck_hits=[_current_stuck_hit(), _legacy_stuck_hit()],
    )


class TestMixedBlobRoundTrip:
    """Both row types out of one blob, each keeping its own meaning of z_score."""

    def test_the_statistical_z_score_survives_as_a_z_score(self, mixed_portion) -> None:
        restored = _rehydrate(_blob(mixed_portion))
        assert [hit.z_score for hit in restored.stat_hits] == [STAT_Z_SCORE]

    def test_the_current_stuck_row_keeps_a_null_z_score(self, mixed_portion) -> None:
        restored = _rehydrate(_blob(mixed_portion))
        current = restored.stuck_hits[0]
        assert current.z_score is None
        assert current.frozen_hours_at_detection == GATE_HOURS

    def test_the_legacy_stuck_row_keeps_its_duration_in_z_score(
        self, mixed_portion
    ) -> None:
        """No silent migration on read: the field it was written in is the
        field it comes back in, and the new field stays ``None``."""
        restored = _rehydrate(_blob(mixed_portion))
        legacy = restored.stuck_hits[1]
        assert legacy.z_score == GATE_HOURS
        assert legacy.frozen_hours_at_detection is None

    def test_the_two_stuck_shapes_stay_distinguishable(self, mixed_portion) -> None:
        """A reader can tell which field to trust; a backfill would erase this."""
        current, legacy = _rehydrate(_blob(mixed_portion)).stuck_hits
        assert (current.z_score, current.frozen_hours_at_detection) != (
            legacy.z_score,
            legacy.frozen_hours_at_detection,
        )

    def test_the_round_trip_is_lossless_for_every_hit(self, mixed_portion) -> None:
        restored = _rehydrate(_blob(mixed_portion))
        assert [hit.model_dump() for hit in restored.stat_hits] == [
            hit.model_dump() for hit in mixed_portion.stat_hits
        ]
        assert [hit.model_dump() for hit in restored.stuck_hits] == [
            hit.model_dump() for hit in mixed_portion.stuck_hits
        ]

    def test_a_second_round_trip_changes_nothing(self, mixed_portion) -> None:
        """Idempotent: re-storing a rehydrated portion is a fixed point.

        Portions are re-read on every finalize retry, so a lossy read that
        merely looked stable once would drift over retries.
        """
        once = _blob(mixed_portion)
        twice = _blob(_rehydrate(once))
        assert twice == once

    def test_the_stored_blob_names_both_duration_fields_explicitly(
        self, mixed_portion
    ) -> None:
        """The current shape writes ``z_score: null`` rather than omitting it.

        That is what made the display-path ``.get("z_score", 0)`` default
        ineffective — present-but-null, so the default never applied.
        """
        stored_current = _blob(mixed_portion)["stuck_hits"][0]
        assert stored_current["z_score"] is None
        assert "z_score" in stored_current
        assert stored_current["frozen_hours_at_detection"] == GATE_HOURS

    def test_the_legacy_row_is_stored_with_a_null_new_field(
        self, mixed_portion
    ) -> None:
        """A blob written today from a legacy-shaped model is still legacy.

        ``UploadHighAnomalyDetail`` defaults the new field to ``None``, so
        round-tripping a legacy row does not invent a duration for it.
        """
        stored_legacy = _blob(mixed_portion)["stuck_hits"][1]
        assert stored_legacy["frozen_hours_at_detection"] is None
        assert stored_legacy["z_score"] == GATE_HOURS

    def test_a_statistical_row_cannot_hold_the_new_field(self) -> None:
        """``StatisticalAnomalyDetail`` has no ``frozen_hours_at_detection``.

        So the two meanings of ``z_score`` cannot be disambiguated by looking
        for the new field on a statistical row — only the detector can do it.
        """
        assert "frozen_hours_at_detection" not in _stat_hit().model_dump()

    def test_an_empty_stuck_list_round_trips(self) -> None:
        """The common case: most portions have statistical hits and no freeze."""
        portion = PortionScores(stat_hits=[_stat_hit()], stuck_hits=[])
        restored = _rehydrate(_blob(portion))
        assert restored.stuck_hits == []
        assert len(restored.stat_hits) == 1

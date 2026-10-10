"""Item 17: both fired-freeze checks in ``upload_portions`` match by instant.

Two call sites decide what an OPEN freeze suppresses — statistical-hit
suppression (``_suppress_fault_window_hits``) and baseline exclusion
(``_exclude_faults_from_baseline``). Both must ask
``StuckCarry.has_onset_fired``, which canonicalises the onset to UTC before
the lookup, rather than testing ``(channel, onset_ts) in
stuck_carry.emitted_onsets`` directly.

Why it matters: ``emitted_onsets`` is keyed on a timestamp STRING.
``2026-12-18T13:00:00Z`` and ``2026-12-18T13:00:00+00:00`` are the same
instant and different strings, so raw membership answers ``False`` for a
freeze that really did fire — indistinguishable from "never fired", and the
consequence is a silently unsuppressed window rather than an error. Today
both call sites happen to build the key from a canonical ``state.onset_ts``;
these tests are what keeps that true once a key arrives off the wire, where
the spelling is not ours to choose.

The carries here are hand-built so the two spellings can be set apart
deliberately; no episode count is taken from them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.api.schemas.sensor_data import StatisticalAnomalyDetail
from src.api.services.upload_portions import (
    StatCarry,
    _exclude_faults_from_baseline,
    _suppress_fault_window_hits,
)
from src.models.stuck_fault_detector import ChannelFreezeState, StuckCarry

CHANNEL = "humidity"
FROZEN_VALUE = 100.0
ONSET = datetime(2026, 12, 18, 13, 0, tzinfo=timezone.utc)
LAST_FROZEN = ONSET + timedelta(hours=8)

# The same instant as ONSET, in the spellings a serialised carry or an
# upstream producer can legitimately present.
ONSET_SPELLINGS = (
    "2026-12-18T13:00:00+00:00",
    "2026-12-18T13:00:00Z",
    "2026-12-18T13:00:00",
    "2026-12-18T14:00:00+01:00",
)


def _open_fired_carry(onset_spelling: str) -> StuckCarry:
    """A carry with one OPEN freeze that fired, keyed on *onset_spelling*.

    The state's ``onset_ts`` carries the alternative spelling while
    ``emitted_onsets`` holds the canonical one — the shape a carry takes when
    the key was written by one process and read by another.
    """
    state = ChannelFreezeState(
        frozen_value=FROZEN_VALUE,
        onset_ts=onset_spelling,
        onset_dt=ONSET,
        last_ts=LAST_FROZEN.isoformat(),
        last_dt=LAST_FROZEN,
        sample_count=97,
        cadence_seconds=900.0,
        cadence_branch="pre_onset",
    )
    carry = StuckCarry(channels={CHANNEL: state})
    carry.emitted_onsets.add((CHANNEL, ONSET.isoformat()))
    return carry


def _open_unfired_carry() -> StuckCarry:
    """The same open freeze, but one that never crossed the gate."""
    carry = _open_fired_carry(ONSET.isoformat())
    carry.emitted_onsets.clear()
    return carry


def _stat_hit(timestamp: datetime, *, column: str = CHANNEL) -> StatisticalAnomalyDetail:
    return StatisticalAnomalyDetail(
        timestamp=timestamp.isoformat(),
        column=column,
        value=FROZEN_VALUE,
        z_score=7.5,
        severity="high",
        method="zscore",
    )


def _raw_row(timestamp: datetime) -> dict[str, Any]:
    return {
        "timestamp": timestamp.isoformat(),
        CHANNEL: FROZEN_VALUE,
        "temperature": 21.0,
    }


# =========================================================================
# Statistical-hit suppression
# =========================================================================


class TestSuppressionMatchesByInstant:
    @pytest.mark.parametrize("onset_spelling", ONSET_SPELLINGS)
    def test_hit_inside_an_open_fired_window_is_suppressed(
        self, onset_spelling: str
    ) -> None:
        """However the onset is spelled, the fired window suppresses the hit."""
        hits = [_stat_hit(ONSET + timedelta(hours=2))]
        assert (
            _suppress_fault_window_hits(hits, _open_fired_carry(onset_spelling)) == []
        )

    def test_hit_is_kept_when_the_freeze_never_fired(self) -> None:
        """A sub-gate freeze creates no suppression window at all.

        This is what makes the test above discriminating: suppression follows
        the fired record, not the mere presence of an open freeze.
        """
        hits = [_stat_hit(ONSET + timedelta(hours=2))]
        assert _suppress_fault_window_hits(hits, _open_unfired_carry()) == hits

    @pytest.mark.parametrize("onset_spelling", ONSET_SPELLINGS)
    def test_hit_before_the_onset_is_kept(self, onset_spelling: str) -> None:
        """An open fired window runs ``[onset, +inf)`` — not backwards."""
        hits = [_stat_hit(ONSET - timedelta(hours=1))]
        assert (
            _suppress_fault_window_hits(hits, _open_fired_carry(onset_spelling))
            == hits
        )

    @pytest.mark.parametrize("onset_spelling", ONSET_SPELLINGS)
    def test_other_channels_are_kept(self, onset_spelling: str) -> None:
        """Only the faulted channel is suppressed, matched by exact column."""
        hits = [_stat_hit(ONSET + timedelta(hours=2), column="temperature")]
        assert (
            _suppress_fault_window_hits(hits, _open_fired_carry(onset_spelling))
            == hits
        )


# =========================================================================
# Baseline exclusion
# =========================================================================


class TestBaselineExclusionMatchesByInstant:
    @pytest.mark.parametrize("onset_spelling", ONSET_SPELLINGS)
    def test_frozen_rows_are_nan_ed_whatever_the_spelling(
        self, onset_spelling: str
    ) -> None:
        """Frozen values inside a fired open window never reach the baseline."""
        rows = [_raw_row(ONSET + timedelta(hours=2))]
        cleaned = _exclude_faults_from_baseline(
            rows, StatCarry(), _open_fired_carry(onset_spelling)
        )
        assert cleaned[0][CHANNEL] != cleaned[0][CHANNEL]  # NaN
        assert cleaned[0]["temperature"] == 21.0

    def test_rows_are_untouched_when_the_freeze_never_fired(self) -> None:
        """No gate crossing, no exclusion — the companion negative case."""
        rows = [_raw_row(ONSET + timedelta(hours=2))]
        cleaned = _exclude_faults_from_baseline(
            rows, StatCarry(), _open_unfired_carry()
        )
        assert cleaned[0][CHANNEL] == FROZEN_VALUE

    @pytest.mark.parametrize("onset_spelling", ONSET_SPELLINGS)
    def test_input_rows_are_not_mutated(self, onset_spelling: str) -> None:
        """Persisted raw readings must survive the baseline cleanup intact."""
        rows = [_raw_row(ONSET + timedelta(hours=2))]
        _exclude_faults_from_baseline(
            rows, StatCarry(), _open_fired_carry(onset_spelling)
        )
        assert rows[0][CHANNEL] == FROZEN_VALUE

    @pytest.mark.parametrize("onset_spelling", ONSET_SPELLINGS)
    def test_carried_stat_rows_are_cleaned_in_place(
        self, onset_spelling: str
    ) -> None:
        """The rolling carry's own rows are cleaned too, not just this portion's."""
        stat_carry = StatCarry(rows=[_raw_row(ONSET + timedelta(hours=3))])
        _exclude_faults_from_baseline(
            [], stat_carry, _open_fired_carry(onset_spelling)
        )
        value = stat_carry.rows[0][CHANNEL]
        assert value != value  # NaN

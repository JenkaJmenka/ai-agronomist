"""Detector-level acceptance for episode eligibility and the two sample counts.

Items 11-15 and item 17's ``from_dict`` warning, from
``docs/handoff/task3_part_c.md``:

* 11 — only a freeze past the DUAL gate (hours AND samples) is exposed as an
  episode, and both counts are computed inside the detector.
* 12 — cadence is pinned once per freeze, by branch 1 (pre-onset median) when
  there is pre-onset evidence and by branch 2 (in-freeze median at gate fire)
  otherwise. ``CADENCE_EXPECTED_SECONDS`` is never a fired freeze's cadence.
* 13 — ``raw_slot_count`` is EXPECTED slots at the pinned cadence, so the gap
  against ``frozen_sample_count`` equals the readings missing from the freeze.
* 14 — a freeze split across async portions, with the split INSIDE the freeze
  and before the gate fires, agrees with a single pass.
* 15 — ``has_onset_fired`` matches by instant, not by spelling.

These tests drive ``WallclockStuckDetector`` directly over synthetic frames
rather than through an upload path, so every carry here is built with
``skip_channels`` covering the channels the frame does not carry — which is
what all three upload entry points do
(``cold_run_upload.py``, ``upload_producer.py``, ``upload_consumer.py``). A
bare ``StuckCarry()`` plus a constant-filled optional channel is what
manufactures a phantom file-long episode; the frames here carry only the
channels under test, and the skip set is explicit so the omission cannot be
read as an oversight.

Cadences used are 60 s and 900 s, never 300 s: 300 s is
``CADENCE_EXPECTED_SECONDS``, so a cadence measured from a 300 s frame would
be indistinguishable from the constant the pinning branches exist to avoid.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest

from src.config import (
    CADENCE_EXPECTED_SECONDS,
    CADENCE_PIN_MIN_INTERVALS,
    STUCK_WALLCLOCK_CHANNELS,
    STUCK_WALLCLOCK_MIN_HOURS,
    STUCK_WALLCLOCK_MIN_SAMPLES,
)
from src.models.stuck_fault_detector import (
    CADENCE_BRANCH_CARRIED,
    CADENCE_BRANCH_IN_FREEZE,
    CADENCE_BRANCH_PRE_ONSET,
    ChannelFreezeState,
    StuckCarry,
    WallclockStuckDetector,
    expected_slot_count,
)

CHANNEL = "humidity"
FROZEN_VALUE = 100.0
BASE_TS = datetime(2027, 1, 5, 0, 0, tzinfo=timezone.utc)

DETECTOR_MODULE_LOGGER = "src.models.stuck_fault_detector"


def _carry() -> StuckCarry:
    """A carry built the way the upload paths build it, for humidity only.

    Every monitored channel the frames below do not carry is skipped
    explicitly, so no constant-filled absent channel can open a freeze.
    """
    return StuckCarry(
        skip_channels=frozenset(
            channel for channel in STUCK_WALLCLOCK_CHANNELS if channel != CHANNEL
        )
    )


def _frame(
    *,
    cadence_seconds: float,
    frozen_rows: int,
    lead_rows: int = 0,
    drop_offsets: tuple[int, ...] = (),
    release: bool = False,
    start_ts: datetime = BASE_TS,
) -> pd.DataFrame:
    """A single-channel frame holding one freeze.

    *lead_rows* varying readings come first (branch 1's pre-onset evidence),
    then *frozen_rows* readings at ``FROZEN_VALUE``, then optionally one
    changed reading that releases the freeze. *drop_offsets* are row offsets
    WITHIN the freeze to omit, standing in for readings that never arrived;
    offset 0 and the last offset are rejected, because removing an endpoint
    would move the freeze's span rather than leave a gap inside it.
    """
    for offset in drop_offsets:
        assert 0 < offset < frozen_rows - 1, (
            f"drop offset {offset} is an endpoint of the freeze, which would "
            "shorten its span instead of leaving a gap inside it"
        )

    step = timedelta(seconds=cadence_seconds)
    rows: list[dict[str, Any]] = []
    for index in range(lead_rows):
        rows.append(
            {
                "timestamp": start_ts + index * step,
                CHANNEL: FROZEN_VALUE - 10.0 - index,
            }
        )
    for offset in range(frozen_rows):
        if offset in drop_offsets:
            continue
        rows.append(
            {
                "timestamp": start_ts + (lead_rows + offset) * step,
                CHANNEL: FROZEN_VALUE,
            }
        )
    if release:
        rows.append(
            {
                "timestamp": start_ts + (lead_rows + frozen_rows) * step,
                CHANNEL: FROZEN_VALUE - 25.0,
            }
        )
    return pd.DataFrame(rows)


def _consume(
    frames: list[pd.DataFrame],
    *,
    carry: StuckCarry | None = None,
    round_trip: bool = False,
) -> tuple[StuckCarry, list[dict[str, Any]]]:
    """Feed *frames* to one detector, returning the carry and all alerts.

    *round_trip* serialises and deserialises the carry between frames, which
    is what the async upload path does: the carry travels inside each portion
    message rather than being threaded through in memory.
    """
    detector = WallclockStuckDetector()
    carry = carry if carry is not None else _carry()
    alerts: list[dict[str, Any]] = []
    for index, frame in enumerate(frames):
        if round_trip and index > 0:
            carry = StuckCarry.from_dict(carry.to_dict())
        alerts.extend(detector.detect_chunk(frame, carry))
    alerts.extend(detector.flush(carry))
    return carry, alerts


# =========================================================================
# Item 11 — the dual gate decides what becomes an episode
# =========================================================================


class TestDualGate:
    async def test_sub_hours_freeze_is_not_exposed(self) -> None:
        """Enough samples, not enough hours: nothing fires.

        20 readings one minute apart satisfy the sample limb and span 19
        minutes, far below the 6 h limb.
        """
        carry, alerts = _consume([_frame(cadence_seconds=60, frozen_rows=20)])
        assert alerts == []
        assert carry.emitted_onsets == set()
        assert carry.completed_faults == []

    async def test_sub_samples_freeze_is_not_exposed(self) -> None:
        """Enough hours, not enough samples: nothing fires.

        8 hourly readings span 7 h — past the hours limb — but 8 is below the
        12-sample limb, which is the only reason this freeze is invisible.
        """
        frame = _frame(cadence_seconds=3600, frozen_rows=8)
        span_hours = (
            frame["timestamp"].iloc[-1] - frame["timestamp"].iloc[0]
        ).total_seconds() / 3600.0
        assert span_hours > STUCK_WALLCLOCK_MIN_HOURS
        assert len(frame) < STUCK_WALLCLOCK_MIN_SAMPLES

        carry, alerts = _consume([frame])
        assert alerts == []
        assert carry.emitted_onsets == set()

    async def test_freeze_past_both_limbs_fires_once(self) -> None:
        """A freeze past both limbs produces exactly one alert, not one per row."""
        carry, alerts = _consume([_frame(cadence_seconds=900, frozen_rows=40)])
        assert len(alerts) == 1
        assert alerts[0]["channel"] == CHANNEL
        assert alerts[0]["severity"] == "urgent"
        assert carry.has_onset_fired(CHANNEL, BASE_TS.isoformat())

    async def test_unfired_freeze_leaves_no_fault_window(self) -> None:
        """A sub-gate freeze that releases records no window to persist."""
        carry, alerts = _consume(
            [_frame(cadence_seconds=60, frozen_rows=20, release=True)]
        )
        assert alerts == []
        assert carry.completed_faults == []

    async def test_fired_freeze_window_is_marked_fired(self) -> None:
        """The window of a fired freeze carries ``has_fired=True``."""
        carry, _ = _consume(
            [_frame(cadence_seconds=900, frozen_rows=40, release=True)]
        )
        assert len(carry.completed_faults) == 1
        assert carry.completed_faults[0].has_fired is True

    async def test_counts_come_from_the_detector_not_the_frame(self) -> None:
        """Both counts are the detector's own tally over the frozen span.

        The frame holds 40 frozen readings plus a lead and a release row, so a
        count taken from the frame's length would be 42, and one taken from
        its span would include the release.
        """
        frame = _frame(
            cadence_seconds=900, frozen_rows=40, lead_rows=1, release=True
        )
        carry, _ = _consume([frame])
        window = carry.completed_faults[0]

        assert window.frozen_sample_count == 40
        assert len(frame) == 42
        assert window.raw_slot_count == expected_slot_count(
            window.onset_dt, window.last_frozen_dt, 900.0
        )
        assert window.last_frozen_ts != window.release_ts


# =========================================================================
# Item 12 — which branch pins the cadence
# =========================================================================


class TestCadencePinning:
    @pytest.mark.parametrize("cadence_seconds", (60.0, 900.0))
    async def test_branch_one_pins_the_pre_onset_median(
        self, cadence_seconds: float
    ) -> None:
        """With pre-onset evidence, the freeze pins that median at onset."""
        lead_rows = CADENCE_PIN_MIN_INTERVALS + 2
        carry, _ = _consume(
            [
                _frame(
                    cadence_seconds=cadence_seconds,
                    frozen_rows=400,
                    lead_rows=lead_rows,
                )
            ]
        )
        state = carry.channels[CHANNEL]
        assert state.cadence_branch == CADENCE_BRANCH_PRE_ONSET
        assert state.cadence_seconds == cadence_seconds

    @pytest.mark.parametrize("cadence_seconds", (60.0, 900.0))
    async def test_branch_two_pins_the_in_freeze_median_at_gate_fire(
        self, cadence_seconds: float
    ) -> None:
        """A freeze opening on the first row has no pre-onset evidence.

        It pins the median of its own intervals instead, and only when the
        gate fires.
        """
        carry, alerts = _consume(
            [_frame(cadence_seconds=cadence_seconds, frozen_rows=400)]
        )
        assert len(alerts) == 1
        state = carry.channels[CHANNEL]
        assert state.cadence_branch == CADENCE_BRANCH_IN_FREEZE
        assert state.cadence_seconds == cadence_seconds

    async def test_branch_two_freeze_is_unpinned_before_the_gate_fires(
        self,
    ) -> None:
        """Until the gate fires there is no pinned cadence, only a placeholder.

        ``cadence_branch`` is the authoritative "has this pinned?" — the
        ``cadence_seconds`` field default is a placeholder, not an answer.
        """
        carry, alerts = _consume([_frame(cadence_seconds=900, frozen_rows=10)])
        assert alerts == []
        state = carry.channels[CHANNEL]
        assert state.cadence_branch is None
        assert state.in_freeze_intervals == [900.0] * 9

    async def test_pinned_cadence_is_not_recomputed_as_the_freeze_grows(
        self,
    ) -> None:
        """Once pinned, the cadence survives a change in the observed rate.

        The freeze opens at 900 s, fires, and then continues at 60 s. Its
        ``raw_slot_count`` must keep scaling by the pinned 900 s, or the count
        would depend on how much of the file had been read.
        """
        slow = _frame(cadence_seconds=900, frozen_rows=40)
        fast_start = slow["timestamp"].iloc[-1] + timedelta(seconds=60)
        fast = _frame(
            cadence_seconds=60, frozen_rows=30, start_ts=fast_start, release=True
        )

        carry, alerts = _consume([slow, fast])
        assert len(alerts) == 1
        window = carry.completed_faults[0]
        assert window.raw_slot_count == expected_slot_count(
            window.onset_dt, window.last_frozen_dt, 900.0
        )
        assert window.raw_slot_count != expected_slot_count(
            window.onset_dt, window.last_frozen_dt, 60.0
        )

    @pytest.mark.parametrize("cadence_seconds", (60.0, 900.0))
    @pytest.mark.parametrize("lead_rows", (0, CADENCE_PIN_MIN_INTERVALS + 2))
    async def test_expected_constant_is_never_a_fired_cadence(
        self, cadence_seconds: float, lead_rows: int
    ) -> None:
        """No fired freeze reports ``CADENCE_EXPECTED_SECONDS`` as its cadence.

        Both pinning branches measure, so the constant scales no persisted
        ``raw_slot_count`` — which is the whole point of pinning: scoring a
        900 s file against the 300 s default invents missing readings.
        """
        carry, alerts = _consume(
            [
                _frame(
                    cadence_seconds=cadence_seconds,
                    frozen_rows=400,
                    lead_rows=lead_rows,
                )
            ]
        )
        assert len(alerts) == 1
        state = carry.channels[CHANNEL]
        assert state.cadence_branch is not None
        assert state.cadence_seconds == cadence_seconds
        assert state.cadence_seconds != float(CADENCE_EXPECTED_SECONDS)


# =========================================================================
# Item 13 — the gap is the readings that never arrived
# =========================================================================


class TestRawSlotGap:
    @pytest.mark.parametrize(
        ("cadence_seconds", "frozen_rows", "drop_offsets"),
        (
            (900.0, 40, (7, 15, 23, 31)),
            (60.0, 400, (40, 90, 140, 190, 240, 290, 340)),
        ),
    )
    async def test_gap_equals_the_rows_removed(
        self,
        cadence_seconds: float,
        frozen_rows: int,
        drop_offsets: tuple[int, ...],
    ) -> None:
        """Freeze at the first rows: the gap is exactly the deleted readings.

        The removed rows are interior to the freeze, so its span — and
        therefore its expected slot count — is unchanged, while the observed
        frozen count drops by one per removal.
        """
        frame = _frame(
            cadence_seconds=cadence_seconds,
            frozen_rows=frozen_rows,
            drop_offsets=drop_offsets,
            release=True,
        )
        carry, alerts = _consume([frame])
        assert len(alerts) == 1

        window = carry.completed_faults[0]
        assert window.raw_slot_count == frozen_rows
        assert window.frozen_sample_count == frozen_rows - len(drop_offsets)
        assert (
            window.raw_slot_count - window.frozen_sample_count
        ) == len(drop_offsets)

    @pytest.mark.parametrize("cadence_seconds", (60.0, 900.0))
    async def test_complete_freeze_has_no_gap(self, cadence_seconds: float) -> None:
        """Nothing missing → the two counts agree, whatever the cadence."""
        carry, _ = _consume(
            [_frame(cadence_seconds=cadence_seconds, frozen_rows=400, release=True)]
        )
        window = carry.completed_faults[0]
        assert window.raw_slot_count == window.frozen_sample_count == 400

    async def test_a_null_reading_inside_the_freeze_shows_as_a_gap(self) -> None:
        """A null is a reading the channel owed and did not deliver.

        The detector skips it rather than breaking the freeze, so the span
        still covers it and the expected-slot count still counts it.
        """
        frame = _frame(cadence_seconds=900, frozen_rows=40, release=True)
        frame.loc[frame.index[10], CHANNEL] = float("nan")

        carry, _ = _consume([frame])
        window = carry.completed_faults[0]
        assert window.frozen_sample_count == 39
        assert window.raw_slot_count == 40


# =========================================================================
# Item 14 — a freeze split across portions agrees with a single pass
# =========================================================================


class TestSplitFreezeParity:
    @staticmethod
    def _summary(carry: StuckCarry, alerts: list[dict[str, Any]]) -> dict[str, Any]:
        """The facts a persisted episode is built from."""
        window = carry.completed_faults[0]
        state = carry.channels[CHANNEL]
        return {
            "alerts": [
                (alert["timestamp"], alert["detected_ts"], alert["value"])
                for alert in alerts
            ],
            "onset_ts": window.onset_ts,
            "last_frozen_ts": window.last_frozen_ts,
            "release_ts": window.release_ts,
            "frozen_sample_count": window.frozen_sample_count,
            "raw_slot_count": window.raw_slot_count,
            "cadence_seconds": state.cadence_seconds,
            "cadence_branch": state.cadence_branch,
        }

    @pytest.mark.parametrize("lead_rows", (0, CADENCE_PIN_MIN_INTERVALS + 2))
    @pytest.mark.parametrize("split_at", (3, 11, 19))
    async def test_split_inside_the_freeze_matches_single_pass(
        self, lead_rows: int, split_at: int
    ) -> None:
        """Splitting before the gate fires changes nothing about the episode.

        The gate needs 6 h, which at 900 s is row 24, so every split point
        here falls inside the freeze and before the fire. The carry is
        serialised and deserialised between portions, exactly as the async
        upload path ships it in each message.
        """
        frame = _frame(
            cadence_seconds=900, frozen_rows=40, lead_rows=lead_rows, release=True
        )
        single_carry, single_alerts = _consume([frame])
        split_carry, split_alerts = _consume(
            [frame.iloc[:split_at].copy(), frame.iloc[split_at:].copy()],
            round_trip=True,
        )

        assert len(single_alerts) == 1
        assert self._summary(split_carry, split_alerts) == self._summary(
            single_carry, single_alerts
        )

    async def test_split_freeze_with_a_gap_keeps_the_same_counts(self) -> None:
        """Missing readings straddling the split are still counted once."""
        frame = _frame(
            cadence_seconds=900,
            frozen_rows=40,
            drop_offsets=(7, 15, 23),
            release=True,
        )
        single_carry, single_alerts = _consume([frame])
        split_carry, split_alerts = _consume(
            [frame.iloc[:9].copy(), frame.iloc[9:].copy()], round_trip=True
        )

        assert self._summary(split_carry, split_alerts) == self._summary(
            single_carry, single_alerts
        )
        assert split_carry.completed_faults[0].raw_slot_count == 40
        assert split_carry.completed_faults[0].frozen_sample_count == 37

    async def test_one_row_per_portion_matches_single_pass(self) -> None:
        """The degenerate split: every row its own portion."""
        frame = _frame(cadence_seconds=900, frozen_rows=40, release=True)
        single_carry, single_alerts = _consume([frame])
        split_carry, split_alerts = _consume(
            [frame.iloc[index : index + 1].copy() for index in range(len(frame))],
            round_trip=True,
        )
        assert self._summary(split_carry, split_alerts) == self._summary(
            single_carry, single_alerts
        )


# =========================================================================
# Item 15 — has_onset_fired matches by instant, not by spelling
# =========================================================================


class TestHasOnsetFiredSpelling:
    @staticmethod
    def _fired_carry() -> StuckCarry:
        """A carry whose one freeze fired, keyed on a ``+00:00`` onset."""
        onset = datetime(2026, 12, 18, 13, 0, tzinfo=timezone.utc)
        state = ChannelFreezeState(
            frozen_value=FROZEN_VALUE,
            onset_ts=onset.isoformat(),
            onset_dt=onset,
            last_ts=(onset + timedelta(hours=8)).isoformat(),
            last_dt=onset + timedelta(hours=8),
            sample_count=97,
            cadence_seconds=900.0,
            cadence_branch=CADENCE_BRANCH_PRE_ONSET,
        )
        carry = StuckCarry(channels={CHANNEL: state})
        carry.mark_onset_fired(CHANNEL, state)
        return carry

    @pytest.mark.parametrize(
        "spelling",
        (
            "2026-12-18T13:00:00+00:00",
            "2026-12-18T13:00:00Z",
            "2026-12-18T13:00:00",
            "2026-12-18T14:00:00+01:00",
            "2026-12-18T08:00:00-05:00",
        ),
    )
    async def test_every_spelling_of_the_instant_matches(
        self, spelling: str
    ) -> None:
        """A ``Z`` suffix, a bare naive stamp and an offset all find the key.

        A miss here is indistinguishable from "this freeze never fired", which
        would silently drop the episode rather than fail.
        """
        assert self._fired_carry().has_onset_fired(CHANNEL, spelling) is True

    @pytest.mark.parametrize(
        "spelling",
        ("2026-12-18T13:05:00+00:00", "2026-12-18T13:00:00+02:00"),
    )
    async def test_a_different_instant_does_not_match(self, spelling: str) -> None:
        """Canonicalising must not collapse distinct instants together."""
        assert self._fired_carry().has_onset_fired(CHANNEL, spelling) is False

    async def test_another_channel_does_not_match(self) -> None:
        """The key is (channel, instant): the channel still has to agree."""
        assert (
            self._fired_carry().has_onset_fired(
                "temperature", "2026-12-18T13:00:00Z"
            )
            is False
        )

    @pytest.mark.parametrize(
        "spelling", ("2026-12-18T13:00:00Z", "2026-12-18T13:00:00")
    )
    async def test_spelling_survives_the_carry_round_trip(
        self, spelling: str
    ) -> None:
        """A carry serialised with a ``Z``-spelled key is matchable after it.

        The async path reconstructs the carry from a message, so the keys the
        worker looks up are the ones ``from_dict`` restored.
        """
        state = self._fired_carry().channels[CHANNEL]
        raw = StuckCarry(channels={CHANNEL: state}).to_dict()
        raw["emitted_onsets"] = [[CHANNEL, spelling]]

        restored = StuckCarry.from_dict(raw)
        assert restored.has_onset_fired(CHANNEL, "2026-12-18T13:00:00+00:00") is True

    async def test_an_unparseable_key_still_matches_itself(self) -> None:
        """An unparseable stamp falls back to exact match, not to a collapse."""
        restored = StuckCarry.from_dict(
            {"emitted_onsets": [[CHANNEL, "not-a-timestamp"]]}
        )
        assert restored.has_onset_fired(CHANNEL, "not-a-timestamp") is True
        assert restored.has_onset_fired(CHANNEL, "also-not-a-timestamp") is False


# =========================================================================
# Item 17 (second half) — a carried state with no pinned cadence warns
# =========================================================================


class TestCarriedCadenceWarning:
    @staticmethod
    def _state_dict(**overrides: Any) -> dict[str, Any]:
        """A serialised freeze state, minus whichever cadence keys are dropped."""
        onset = datetime(2027, 2, 2, 4, 0, tzinfo=timezone.utc)
        state: dict[str, Any] = {
            "frozen_value": FROZEN_VALUE,
            "onset_ts": onset.isoformat(),
            "onset_dt": onset.isoformat(),
            "last_ts": (onset + timedelta(hours=8)).isoformat(),
            "last_dt": (onset + timedelta(hours=8)).isoformat(),
            "sample_count": 97,
        }
        state.update(overrides)
        return state

    def test_absent_cadence_logs_a_warning_with_the_freeze_identity(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Deploy skew is tolerated but never silent.

        Asserted on the record's ``args`` rather than the formatted message, so
        rewording the warning does not break this test.
        """
        with caplog.at_level(logging.WARNING, logger=DETECTOR_MODULE_LOGGER):
            StuckCarry.from_dict({"channels": {CHANNEL: self._state_dict()}})

        warnings = [
            record
            for record in caplog.records
            if record.name == DETECTOR_MODULE_LOGGER
            and record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert warnings[0].args == (
            CHANNEL,
            "2027-02-02T04:00:00+00:00",
            CADENCE_EXPECTED_SECONDS,
        )

    def test_absent_cadence_leaves_the_freeze_unpinned(self) -> None:
        """The constant is a placeholder, so the freeze can still pin later."""
        carry = StuckCarry.from_dict(
            {"channels": {CHANNEL: self._state_dict()}}
        )
        state = carry.channels[CHANNEL]
        assert state.cadence_branch is None
        assert state.cadence_seconds == float(CADENCE_EXPECTED_SECONDS)

    def test_carried_cadence_without_a_branch_label_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A producer that pinned before the label existed carried a real value.

        It is marked ``carried`` so it is never recomputed — re-pinning
        mid-freeze is what makes a split run disagree with a single pass.
        """
        with caplog.at_level(logging.WARNING, logger=DETECTOR_MODULE_LOGGER):
            carry = StuckCarry.from_dict(
                {"channels": {CHANNEL: self._state_dict(cadence_seconds=900.0)}}
            )

        state = carry.channels[CHANNEL]
        assert state.cadence_branch == CADENCE_BRANCH_CARRIED
        assert state.cadence_seconds == 900.0
        assert [
            record for record in caplog.records
            if record.name == DETECTOR_MODULE_LOGGER
            and record.levelno == logging.WARNING
        ] == []

    def test_an_unpinned_branch_is_preserved_not_warned_about(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An explicit ``cadence_branch: None`` is the producer's own answer."""
        with caplog.at_level(logging.WARNING, logger=DETECTOR_MODULE_LOGGER):
            carry = StuckCarry.from_dict(
                {
                    "channels": {
                        CHANNEL: self._state_dict(
                            cadence_seconds=float(CADENCE_EXPECTED_SECONDS),
                            cadence_branch=None,
                        )
                    }
                }
            )

        assert carry.channels[CHANNEL].cadence_branch is None
        assert [
            record for record in caplog.records
            if record.name == DETECTOR_MODULE_LOGGER
            and record.levelno == logging.WARNING
        ] == []


# =========================================================================
# A carried freeze that already satisfies the gate
# =========================================================================


class TestCarriedFreezeFiringAtFlush:
    """A freeze restored from a carry can cross the gate at end of input.

    The detector fires the gate on every frozen row it consumes, so a freeze
    it opened itself never reaches ``flush`` still unfired. A state that
    arrived in a portion message can: the rows that made it gate-worthy were
    consumed by another process.
    """

    @staticmethod
    def _gate_worthy_state(**overrides: Any) -> ChannelFreezeState:
        onset = datetime(2027, 5, 1, 0, 0, tzinfo=timezone.utc)
        state = ChannelFreezeState(
            frozen_value=FROZEN_VALUE,
            onset_ts=onset.isoformat(),
            onset_dt=onset,
            last_ts=(onset + timedelta(hours=8)).isoformat(),
            last_dt=onset + timedelta(hours=8),
            sample_count=STUCK_WALLCLOCK_MIN_SAMPLES + 5,
        )
        for field_name, value in overrides.items():
            setattr(state, field_name, value)
        return state

    def test_flush_fires_the_gate_for_a_carried_freeze(self) -> None:
        """The alert is emitted once, stamped at the last frozen sample."""
        state = self._gate_worthy_state(
            cadence_seconds=900.0, cadence_branch=CADENCE_BRANCH_CARRIED
        )
        carry = StuckCarry(channels={CHANNEL: state})

        alerts = WallclockStuckDetector().flush(carry)
        assert len(alerts) == 1
        assert alerts[0]["timestamp"] == state.onset_ts
        assert alerts[0]["detected_ts"] == state.last_ts
        assert carry.has_onset_fired(CHANNEL, state.onset_ts)

    def test_flush_fires_a_freeze_only_once(self) -> None:
        """A second flush must not re-emit an already-fired freeze."""
        carry = StuckCarry(
            channels={
                CHANNEL: self._gate_worthy_state(
                    cadence_seconds=900.0, cadence_branch=CADENCE_BRANCH_CARRIED
                )
            }
        )
        detector = WallclockStuckDetector()
        assert len(detector.flush(carry)) == 1
        assert detector.flush(carry) == []

    def test_unpinned_carried_freeze_warns_that_its_count_is_a_guess(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Firing with no in-freeze intervals leaves the cadence unmeasured.

        Unreachable for a freeze the detector opened — the gate needs
        ``STUCK_WALLCLOCK_MIN_SAMPLES`` samples and every one after the first
        contributes an interval — so this records a hand-built or legacy state,
        whose ``raw_slot_count`` is then scaled by a placeholder.
        """
        state = self._gate_worthy_state()
        carry = StuckCarry(channels={CHANNEL: state})

        with caplog.at_level(logging.WARNING, logger=DETECTOR_MODULE_LOGGER):
            alerts = WallclockStuckDetector().flush(carry)

        assert len(alerts) == 1
        assert state.cadence_branch is None
        warnings = [
            record
            for record in caplog.records
            if record.name == DETECTOR_MODULE_LOGGER
            and record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert warnings[0].args == (
            CHANNEL,
            state.onset_ts,
            float(CADENCE_EXPECTED_SECONDS),
        )


# =========================================================================
# Input the detector must survive without opening or closing a freeze
# =========================================================================


class TestUnusableRows:
    def test_an_empty_frame_changes_nothing(self) -> None:
        """A portion with no rows is not an error and opens no freeze."""
        carry = _carry()
        alerts = WallclockStuckDetector().detect_chunk(pd.DataFrame(), carry)
        assert alerts == []
        assert carry.channels == {}

    def test_a_frame_without_timestamps_changes_nothing(self) -> None:
        """Without a timestamp column there is no wall-clock to gate on."""
        carry = _carry()
        frame = pd.DataFrame([{CHANNEL: FROZEN_VALUE}] * 20)
        assert WallclockStuckDetector().detect_chunk(frame, carry) == []
        assert carry.channels == {}

    def test_rows_with_an_unusable_timestamp_are_skipped(self) -> None:
        """A null or unparseable stamp is dropped, not read as the freeze's own.

        The surrounding readings still form one freeze across the hole: a
        skipped row must not break the freeze or move its span.
        """
        frame = _frame(cadence_seconds=900, frozen_rows=40, release=True)
        # Object dtype so a datetime column can hold the unparseable string
        # a real CSV presents here.
        frame["timestamp"] = frame["timestamp"].astype(object)
        frame.loc[frame.index[5], "timestamp"] = None
        frame.loc[frame.index[9], "timestamp"] = "not-a-timestamp"

        carry = _carry()
        detector = WallclockStuckDetector()
        alerts = detector.detect_chunk(frame, carry)

        assert len(alerts) == 1
        window = carry.completed_faults[0]
        assert window.frozen_sample_count == 38
        assert window.raw_slot_count == 40

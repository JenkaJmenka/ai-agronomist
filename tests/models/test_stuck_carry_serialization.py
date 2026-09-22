"""Round-trip serialisation tests for StuckCarry, ChannelFreezeState, FaultWindow.

These guard the to_dict/from_dict helpers that allow the async upload path
(RabbitMQ workers) to embed the wall-clock stuck-fault carry state in each
portion message.  Portions may arrive out of order or concurrently, so the
carry must be pre-computed by the sequential producer and shipped with each
message — the round-trip must be exact.

Invariants tested:
  - All datetime fields survive with UTC timezone preserved.
  - emitted_onsets round-trips as set of 2-tuples (not list of lists).
  - skip_channels round-trips as frozenset.
  - completed_faults, recovery_channels, channels all reconstruct correctly.
  - An empty (default) StuckCarry round-trips cleanly.
  - A carry with partial / None datetime fields (last_frozen_dt=None) is safe.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models.stuck_fault_detector import (
    ChannelFreezeState,
    FaultWindow,
    StuckCarry,
)


def _utc(year, month, day, hour=0, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_full_carry() -> StuckCarry:
    """Return a StuckCarry with every field populated."""
    onset_dt = _utc(2026, 12, 18, 13, 0, 0)
    last_dt = _utc(2026, 12, 18, 19, 0, 0)
    end_dt = _utc(2026, 12, 25, 8, 55, 0)

    carry = StuckCarry(
        channels={
            "humidity": ChannelFreezeState(
                frozen_value=100.0,
                onset_ts="2026-12-18T13:00:00+00:00",
                onset_dt=onset_dt,
                last_ts="2026-12-18T19:00:00+00:00",
                last_dt=last_dt,
                sample_count=72,
            ),
            "temperature": ChannelFreezeState(
                frozen_value=22.5,
                onset_ts="2026-12-14T00:00:00+00:00",
                onset_dt=_utc(2026, 12, 14),
                last_ts="2026-12-14T06:00:00+00:00",
                last_dt=_utc(2026, 12, 14, 6),
                sample_count=12,
            ),
        },
        chunk_medians=[300.0, 300.0, 299.9],
        last_timestamp_dt=last_dt,
        is_cadence_flagged=False,
        cadence_median_seconds=300.0,
        emitted_onsets={
            ("humidity", "2026-12-14T00:00:00+00:00"),
            ("humidity", "2026-12-15T11:05:00+00:00"),
        },
        skip_channels=frozenset({"co2", "light"}),
        recovery_channels={
            "humidity": FaultWindow(
                channel="humidity",
                onset_ts="2026-12-14T00:00:00+00:00",
                onset_dt=_utc(2026, 12, 14),
                release_ts="2026-12-15T10:55:00+00:00",
                release_dt=_utc(2026, 12, 15, 10, 55),
                last_frozen_ts="2026-12-15T10:50:00+00:00",
                last_frozen_dt=_utc(2026, 12, 15, 10, 50),
            )
        },
        completed_faults=[
            FaultWindow(
                channel="humidity",
                onset_ts="2026-12-14T00:00:00+00:00",
                onset_dt=_utc(2026, 12, 14),
                release_ts="2026-12-15T10:55:00+00:00",
                release_dt=end_dt,
                last_frozen_ts="2026-12-15T10:50:00+00:00",
                last_frozen_dt=_utc(2026, 12, 15, 10, 50),
            )
        ],
    )
    return carry


def _assert_carry_equal(original: StuckCarry, restored: StuckCarry) -> None:
    """Deep-assert equality between two StuckCarry instances."""
    # scalar fields
    assert original.is_cadence_flagged == restored.is_cadence_flagged
    assert original.cadence_median_seconds == restored.cadence_median_seconds
    assert original.chunk_medians == restored.chunk_medians

    # datetime (timezone-aware UTC)
    if original.last_timestamp_dt is None:
        assert restored.last_timestamp_dt is None
    else:
        assert restored.last_timestamp_dt is not None
        assert restored.last_timestamp_dt.tzinfo is not None
        assert original.last_timestamp_dt == restored.last_timestamp_dt

    # skip_channels (frozenset)
    assert isinstance(restored.skip_channels, frozenset)
    assert original.skip_channels == restored.skip_channels

    # emitted_onsets (set of tuples, NOT list of lists)
    assert isinstance(restored.emitted_onsets, set)
    for item in restored.emitted_onsets:
        assert isinstance(item, tuple), f"Expected tuple, got {type(item)}: {item}"
        assert len(item) == 2
    assert original.emitted_onsets == restored.emitted_onsets

    # channels
    assert set(original.channels.keys()) == set(restored.channels.keys())
    for ch in original.channels:
        os = original.channels[ch]
        rs = restored.channels[ch]
        assert os.frozen_value == rs.frozen_value
        assert os.onset_ts == rs.onset_ts
        assert os.last_ts == rs.last_ts
        assert os.sample_count == rs.sample_count
        assert rs.onset_dt.tzinfo is not None
        assert os.onset_dt == rs.onset_dt
        assert rs.last_dt.tzinfo is not None
        assert os.last_dt == rs.last_dt

    # recovery_channels
    assert set(original.recovery_channels.keys()) == set(restored.recovery_channels.keys())
    for ch in original.recovery_channels:
        _assert_fw_equal(original.recovery_channels[ch], restored.recovery_channels[ch])

    # completed_faults
    assert len(original.completed_faults) == len(restored.completed_faults)
    for ofw, rfw in zip(original.completed_faults, restored.completed_faults):
        _assert_fw_equal(ofw, rfw)


def _assert_fw_equal(ofw: FaultWindow, rfw: FaultWindow) -> None:
    assert ofw.channel == rfw.channel
    assert ofw.onset_ts == rfw.onset_ts
    assert ofw.release_ts == rfw.release_ts
    assert ofw.last_frozen_ts == rfw.last_frozen_ts
    assert rfw.onset_dt.tzinfo is not None
    assert ofw.onset_dt == rfw.onset_dt
    assert rfw.release_dt.tzinfo is not None
    assert ofw.release_dt == rfw.release_dt
    assert rfw.last_frozen_dt.tzinfo is not None
    assert ofw.last_frozen_dt == rfw.last_frozen_dt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStuckCarryRoundTrip:
    """StuckCarry.to_dict → from_dict must reproduce the original exactly."""

    def test_empty_carry_round_trips(self):
        carry = StuckCarry()
        restored = StuckCarry.from_dict(carry.to_dict())
        _assert_carry_equal(carry, restored)

    def test_full_carry_round_trips(self):
        carry = _make_full_carry()
        restored = StuckCarry.from_dict(carry.to_dict())
        _assert_carry_equal(carry, restored)

    def test_emitted_onsets_are_tuples_not_lists(self):
        """emitted_onsets must be a set of 2-tuples after deserialisation."""
        carry = StuckCarry(
            emitted_onsets={
                ("humidity", "2026-12-18T13:00:00+00:00"),
                ("temperature", "2026-12-14T00:00:00+00:00"),
            }
        )
        restored = StuckCarry.from_dict(carry.to_dict())
        assert isinstance(restored.emitted_onsets, set)
        for item in restored.emitted_onsets:
            assert isinstance(item, tuple), f"got {type(item)}"
        assert restored.emitted_onsets == carry.emitted_onsets

    def test_skip_channels_is_frozenset(self):
        carry = StuckCarry(skip_channels=frozenset({"co2", "light"}))
        restored = StuckCarry.from_dict(carry.to_dict())
        assert isinstance(restored.skip_channels, frozenset)
        assert restored.skip_channels == carry.skip_channels

    def test_utc_datetimes_preserved(self):
        """Timezone-aware UTC datetimes survive the round-trip exactly."""
        dt = datetime(2026, 12, 18, 13, 0, 0, tzinfo=timezone.utc)
        carry = StuckCarry(
            last_timestamp_dt=dt,
            channels={
                "humidity": ChannelFreezeState(
                    frozen_value=100.0,
                    onset_ts="2026-12-18T13:00:00+00:00",
                    onset_dt=dt,
                    last_ts="2026-12-18T19:00:00+00:00",
                    last_dt=datetime(2026, 12, 18, 19, 0, 0, tzinfo=timezone.utc),
                    sample_count=72,
                )
            },
        )
        restored = StuckCarry.from_dict(carry.to_dict())
        assert restored.last_timestamp_dt == dt
        assert restored.last_timestamp_dt.tzinfo is not None
        assert restored.channels["humidity"].onset_dt == dt
        assert restored.channels["humidity"].onset_dt.tzinfo is not None

    def test_fault_window_last_frozen_fields_required(self):
        """FaultWindow.last_frozen_ts/dt are now required (non-Optional).

        The detector always knows last_frozen at release time.  Verify
        a populated FaultWindow round-trips with all three temporal
        markers intact.
        """
        carry = StuckCarry(
            completed_faults=[
                FaultWindow(
                    channel="humidity",
                    onset_ts="2026-12-14T00:00:00+00:00",
                    onset_dt=_utc(2026, 12, 14),
                    release_ts="2026-12-15T10:55:00+00:00",
                    release_dt=_utc(2026, 12, 15, 10, 55),
                    last_frozen_ts="2026-12-15T10:50:00+00:00",
                    last_frozen_dt=_utc(2026, 12, 15, 10, 50),
                )
            ]
        )
        restored = StuckCarry.from_dict(carry.to_dict())
        assert len(restored.completed_faults) == 1
        assert restored.completed_faults[0].last_frozen_dt == _utc(2026, 12, 15, 10, 50)
        assert restored.completed_faults[0].last_frozen_ts == "2026-12-15T10:50:00+00:00"

    def test_double_round_trip_is_stable(self):
        """Applying to_dict/from_dict twice must produce the same result."""
        carry = _make_full_carry()
        once = StuckCarry.from_dict(carry.to_dict())
        twice = StuckCarry.from_dict(once.to_dict())
        _assert_carry_equal(once, twice)

    def test_no_channels_carry_round_trips(self):
        """A carry with no active channels but with cadence data round-trips."""
        carry = StuckCarry(
            chunk_medians=[300.0, 300.1],
            cadence_median_seconds=300.05,
            is_cadence_flagged=False,
            last_timestamp_dt=_utc(2026, 12, 15, 12, 0, 0),
        )
        restored = StuckCarry.from_dict(carry.to_dict())
        _assert_carry_equal(carry, restored)

    def test_post_release_carry_with_recovery_and_faults(self):
        """Carry after the 12-18 fault released: non-empty recovery_channels AND
        completed_faults with fully populated FaultWindows (including non-None
        last_frozen_ts/dt) round-trip exactly through to_dict/from_dict.

        Task 1c case 8: timezone-aware datetimes survive including in
        recovery_channels and completed_faults.
        """
        carry = StuckCarry(
            channels={
                "humidity": ChannelFreezeState(
                    frozen_value=91.24,
                    onset_ts="2026-12-25T08:55:00+00:00",
                    onset_dt=_utc(2026, 12, 25, 8, 55),
                    last_ts="2026-12-25T09:00:00+00:00",
                    last_dt=_utc(2026, 12, 25, 9, 0),
                    sample_count=2,
                ),
            },
            chunk_medians=[300.0, 300.0, 300.0],
            last_timestamp_dt=_utc(2026, 12, 25, 9, 0),
            is_cadence_flagged=False,
            cadence_median_seconds=300.0,
            emitted_onsets={
                ("humidity", "2026-12-14T00:00:00+00:00"),
                ("humidity", "2026-12-15T11:05:00+00:00"),
                ("humidity", "2026-12-16T11:20:00+00:00"),
                ("humidity", "2026-12-18T13:00:00+00:00"),
            },
            skip_channels=frozenset({"co2"}),
            recovery_channels={
                "humidity": FaultWindow(
                    channel="humidity",
                    onset_ts="2026-12-18T13:00:00+00:00",
                    onset_dt=_utc(2026, 12, 18, 13, 0),
                    release_ts="2026-12-25T08:55:00+00:00",
                    release_dt=_utc(2026, 12, 25, 8, 55),
                    last_frozen_ts="2026-12-25T08:50:00+00:00",
                    last_frozen_dt=_utc(2026, 12, 25, 8, 50),
                ),
            },
            completed_faults=[
                FaultWindow(
                    channel="humidity",
                    onset_ts="2026-12-14T00:00:00+00:00",
                    onset_dt=_utc(2026, 12, 14),
                    release_ts="2026-12-15T10:55:00+00:00",
                    release_dt=_utc(2026, 12, 15, 10, 55),
                    last_frozen_ts="2026-12-15T10:50:00+00:00",
                    last_frozen_dt=_utc(2026, 12, 15, 10, 50),
                ),
                FaultWindow(
                    channel="humidity",
                    onset_ts="2026-12-15T11:05:00+00:00",
                    onset_dt=_utc(2026, 12, 15, 11, 5),
                    release_ts="2026-12-16T09:15:00+00:00",
                    release_dt=_utc(2026, 12, 16, 9, 15),
                    last_frozen_ts="2026-12-16T09:10:00+00:00",
                    last_frozen_dt=_utc(2026, 12, 16, 9, 10),
                ),
                FaultWindow(
                    channel="humidity",
                    onset_ts="2026-12-16T11:20:00+00:00",
                    onset_dt=_utc(2026, 12, 16, 11, 20),
                    release_ts="2026-12-18T11:20:00+00:00",
                    release_dt=_utc(2026, 12, 18, 11, 20),
                    last_frozen_ts="2026-12-18T11:15:00+00:00",
                    last_frozen_dt=_utc(2026, 12, 18, 11, 15),
                ),
                FaultWindow(
                    channel="humidity",
                    onset_ts="2026-12-18T13:00:00+00:00",
                    onset_dt=_utc(2026, 12, 18, 13, 0),
                    release_ts="2026-12-25T08:55:00+00:00",
                    release_dt=_utc(2026, 12, 25, 8, 55),
                    last_frozen_ts="2026-12-25T08:50:00+00:00",
                    last_frozen_dt=_utc(2026, 12, 25, 8, 50),
                ),
            ],
        )
        restored = StuckCarry.from_dict(carry.to_dict())
        _assert_carry_equal(carry, restored)

        # Specifically verify non-None last_frozen_dt round-trips
        for i, (ofw, rfw) in enumerate(
            zip(carry.completed_faults, restored.completed_faults)
        ):
            assert rfw.last_frozen_ts is not None, f"fault {i}: last_frozen_ts is None"
            assert rfw.last_frozen_dt is not None, f"fault {i}: last_frozen_dt is None"
            assert rfw.last_frozen_dt.tzinfo is not None, f"fault {i}: not tz-aware"
            assert ofw.last_frozen_dt == rfw.last_frozen_dt

"""Unit pin: the (channel, onset_ts) lookup key, for BOTH database drivers.

``_augment_stuck_fault_episodes`` matches a query-time stuck-fault episode
to its ``anomaly_episodes`` row on ``(channel, onset_ts)``, with the episode
side of that key built by ``_parse_iso(episode["start_ts"])`` — always
tz-aware UTC.  The record side has to agree, and what the record side
*holds* depends on the driver:

* **SQLite** (every dev and test database) returns a tz-NAIVE ``datetime``
  from a ``DateTime(timezone=True)`` column;
* **asyncpg** (production Postgres) returns a tz-AWARE one.

Keying on the raw value therefore matched on Postgres and never on SQLite,
which is how every stuck fault came to report an unknown span locally while
both branches returned a valid-looking response.
``anomaly_episode_service._as_utc`` normalizes it.

This file exists because the parity suite
(``tests/api/test_episode_view_parity.py``) runs on SQLite only: it covers
the naive branch thoroughly and the aware branch not at all — the mirror
image of the gap that hid the original defect.  Since no test database here
can produce an aware ``onset_ts``, the records are handed to the real
keying code through a minimal session stand-in rather than through a
driver.  That makes this a unit pin of the KEY; the end-to-end behaviour
stays in the parity suite.

Onsets below are fixture A's second freeze (11:05, deliberately not
midnight — a timezone slip at a midnight onset can still land on an equal
date and read as a pass).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.api.services.anomaly_episode_service import (
    _as_utc,
    _augment_stuck_fault_episodes,
    _fetch_stuck_fault_episode_records,
)
from src.db.models import AnomalyEpisodeRecord
from src.models.episode_grouper import _parse_iso

CHANNEL = "humidity"
ONSET_ISO = "2026-12-15T11:05:00+00:00"
END_ISO = "2026-12-16T09:10:00+00:00"
DURATION_SECONDS = 79500
FROZEN_SAMPLE_COUNT = 266
RAW_SLOT_COUNT = 266

# The same instant in the three shapes a record's onset_ts can arrive in.
NAIVE_ONSET = datetime(2026, 12, 15, 11, 5)
UTC_ONSET = datetime(2026, 12, 15, 11, 5, tzinfo=timezone.utc)
OFFSET_ONSET = datetime(
    2026, 12, 15, 13, 5, tzinfo=timezone(timedelta(hours=2))
)

WINDOW_START = datetime(2026, 12, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2027, 1, 15, tzinfo=timezone.utc)

ONSET_SHAPES = (
    pytest.param(NAIVE_ONSET, id="sqlite-naive"),
    pytest.param(UTC_ONSET, id="asyncpg-aware-utc"),
    pytest.param(OFFSET_ONSET, id="aware-non-utc-offset"),
)


class _Result:
    """The two calls ``_fetch_stuck_fault_episode_records`` makes on a result."""

    def __init__(self, records: list[AnomalyEpisodeRecord]) -> None:
        self._records = records

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[AnomalyEpisodeRecord]:
        return list(self._records)


class _RecordsSession:
    """Minimal async session stand-in returning *records* from ``execute``.

    Exists to put a tz-AWARE ``onset_ts`` in front of the real keying code:
    SQLite cannot produce one, and the driver that does needs a live
    Postgres this suite has no access to.  Nothing else about the session is
    simulated — the function under test only queries.
    """

    def __init__(self, records: list[AnomalyEpisodeRecord]) -> None:
        self._records = records
        self.executed: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.executed.append(statement)
        return _Result(self._records)


def _record(onset: datetime) -> AnomalyEpisodeRecord:
    """A transient (never added to a session) stuck-fault episode row."""
    return AnomalyEpisodeRecord(
        tenant_id="keying",
        greenhouse_id="gh_keying",
        channel=CHANNEL,
        detector="stuck_fault",
        fault_type="stuck_fault",
        onset_ts=onset,
        end_ts=datetime.fromisoformat(END_ISO),
        duration_seconds=DURATION_SECONDS,
        frozen_sample_count=FROZEN_SAMPLE_COUNT,
        raw_slot_count=RAW_SLOT_COUNT,
        severity="urgent",
        is_ongoing=False,
    )


async def _records_by_key(onset: datetime) -> dict[tuple[str, datetime], Any]:
    """Run the real keying code over one record shaped like *onset*."""
    return await _fetch_stuck_fault_episode_records(
        _RecordsSession([_record(onset)]),
        tenant_id="keying",
        greenhouse_id="gh_keying",
        channels={CHANNEL},
        start=WINDOW_START,
        end=WINDOW_END,
    )


class TestAsUtc:
    """The normalization rule, stated once and mirrored from ``_parse_iso``."""

    def test_naive_is_read_as_utc(self):
        """SQLite's shape: no offset stored, UTC is what the writer meant."""
        assert _as_utc(NAIVE_ONSET) == UTC_ONSET

    def test_aware_utc_is_unchanged(self):
        """asyncpg's shape: already correct, must survive untouched."""
        assert _as_utc(UTC_ONSET) == UTC_ONSET
        assert _as_utc(UTC_ONSET).tzinfo == timezone.utc

    def test_aware_non_utc_offset_is_converted(self):
        """Converted to UTC, not merely relabelled — 13:05+02:00 is 11:05Z."""
        normalized = _as_utc(OFFSET_ONSET)
        assert normalized == UTC_ONSET
        assert normalized.hour == 11

    def test_every_shape_of_one_instant_normalizes_to_one_value(self):
        """The invariant the lookup depends on: one instant, one key."""
        assert len({_as_utc(onset) for onset in (NAIVE_ONSET, UTC_ONSET, OFFSET_ONSET)}) == 1

    def test_result_is_always_tz_aware(self):
        for onset in (NAIVE_ONSET, UTC_ONSET, OFFSET_ONSET):
            assert _as_utc(onset).tzinfo is not None


class TestLookupKey:
    """The record-side key must match the episode-side key, on every driver."""

    @pytest.mark.parametrize("onset", ONSET_SHAPES)
    async def test_record_key_matches_the_episode_side_key(self, onset: datetime):
        """``_parse_iso`` on the alert's own onset string is the other side."""
        records_by_key = await _records_by_key(onset)
        assert (CHANNEL, _parse_iso(ONSET_ISO)) in records_by_key

    @pytest.mark.parametrize("onset", ONSET_SHAPES)
    async def test_record_key_is_tz_aware(self, onset: datetime):
        keys = list((await _records_by_key(onset)).keys())
        assert len(keys) == 1
        _, keyed_onset = keys[0]
        assert keyed_onset.tzinfo is not None

    @pytest.mark.parametrize("onset", ONSET_SHAPES)
    async def test_channel_rides_along_unmodified(self, onset: datetime):
        records_by_key = await _records_by_key(onset)
        assert [channel for channel, _ in records_by_key] == [CHANNEL]

    async def test_every_driver_shape_produces_the_same_key(self):
        """A record is found by the same key whichever driver returned it."""
        keys = [set((await _records_by_key(onset)).keys()) for onset in
                (NAIVE_ONSET, UTC_ONSET, OFFSET_ONSET)]
        assert keys[0] == keys[1] == keys[2]


class TestAugmentationAcrossDrivers:
    """End of the path: an episode is augmented whatever shape the row had.

    The parity suite proves this for SQLite's naive rows against a real
    database; these cases carry the aware shapes production actually
    returns through the same production code.
    """

    @pytest.mark.parametrize("onset", ONSET_SHAPES)
    async def test_span_and_counts_come_from_the_record(self, onset: datetime):
        episode = {
            "channel": CHANNEL,
            "detector": "stuck_fault",
            "start_ts": ONSET_ISO,
            "end_ts": ONSET_ISO,
            "duration_seconds": 0.0,
            "sample_count": 1,
            "severity": "unclassified",
        }
        await _augment_stuck_fault_episodes(
            _RecordsSession([_record(onset)]),
            [episode],
            tenant_id="keying",
            greenhouse_id="gh_keying",
            start=WINDOW_START,
            end=WINDOW_END,
        )
        assert episode["duration_seconds"] == float(DURATION_SECONDS)
        assert _parse_iso(episode["end_ts"]) == _parse_iso(END_ISO)
        assert episode["frozen_sample_count"] == FROZEN_SAMPLE_COUNT
        assert episode["raw_slot_count"] == RAW_SLOT_COUNT
        assert episode["severity"] == "urgent"

    @pytest.mark.parametrize("onset", ONSET_SHAPES)
    async def test_augmented_span_satisfies_the_duration_invariant(
        self, onset: datetime
    ):
        """``end_ts - start_ts == duration_seconds`` (episode_grouper rule 5)."""
        episode = {
            "channel": CHANNEL,
            "detector": "stuck_fault",
            "start_ts": ONSET_ISO,
            "end_ts": ONSET_ISO,
            "duration_seconds": 0.0,
            "sample_count": 1,
            "severity": "unclassified",
        }
        await _augment_stuck_fault_episodes(
            _RecordsSession([_record(onset)]),
            [episode],
            tenant_id="keying",
            greenhouse_id="gh_keying",
            start=WINDOW_START,
            end=WINDOW_END,
        )
        span = (
            _parse_iso(episode["end_ts"]) - _parse_iso(episode["start_ts"])
        ).total_seconds()
        assert span == episode["duration_seconds"]

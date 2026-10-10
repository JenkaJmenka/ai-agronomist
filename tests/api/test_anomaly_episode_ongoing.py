"""What the episode table records for a freeze still frozen at end of input.

``docs/handoff/task3_4_spec.md`` §4 states the contract explicitly: reaching
end of file does NOT close a freeze. Such an episode is ``is_ongoing = True``,
``release_ts`` NULL, and ``end_ts`` the LAST FROZEN sample — only an actual
release (a first normal sample) flips it closed.

The freeze here is produced by ``WallclockStuckDetector`` over a synthetic
frame and the alert by ``upload_portions._stuck_dict_to_high_detail``, so the
counts and timestamps the writer persists are the detector's own rather than
hand-written. That is the point of the file: the open-freeze branch of
``persist_stuck_fault_episodes`` has no ``FaultWindow`` to read, and the
episode it writes exists nowhere else.

Relevant to Part C item 6: the UPLOAD RESPONSE's episode for an ongoing freeze
reports ``end_ts`` equal to the onset (pinned as a strict xfail in
``tests/workers/test_stuck_async_parity.py``). The persisted row asserted here
is the same freeze seen through the other concept, and it does carry the last
frozen sample.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.anomaly_episode_writer import persist_stuck_fault_episodes
from src.api.services.upload_portions import _stuck_dict_to_high_detail
from src.config import STUCK_WALLCLOCK_CHANNELS
from src.db.models import AnomalyEpisodeRecord, Base
from src.models.stuck_fault_detector import StuckCarry, WallclockStuckDetector

TENANT = "episode_ongoing"
GH_ID = "gh_episode_ongoing"
CHANNEL = "humidity"
FROZEN_VALUE = 65.0
CADENCE_SECONDS = 300.0
FROZEN_ROWS = 80
ONSET = datetime(2026, 12, 20, 0, 0, tzinfo=timezone.utc)
LAST_FROZEN = ONSET + timedelta(seconds=CADENCE_SECONDS * (FROZEN_ROWS - 1))


@pytest.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


def _ongoing_frame() -> pd.DataFrame:
    """80 readings five minutes apart, all frozen: 6 h 35 m, never released."""
    step = timedelta(seconds=CADENCE_SECONDS)
    return pd.DataFrame(
        [
            {"timestamp": ONSET + index * step, CHANNEL: FROZEN_VALUE}
            for index in range(FROZEN_ROWS)
        ]
    )


def _detect_ongoing_freeze() -> tuple[StuckCarry, list[Any]]:
    """Run the detector over the frame and flush it, as an upload would.

    ``skip_channels`` covers every monitored channel the frame does not carry,
    the way all three upload entry points build the carry.
    """
    carry = StuckCarry(
        skip_channels=frozenset(
            channel for channel in STUCK_WALLCLOCK_CHANNELS if channel != CHANNEL
        )
    )
    detector = WallclockStuckDetector()
    alerts = detector.detect_chunk(_ongoing_frame(), carry)
    alerts.extend(detector.flush(carry))
    return carry, [_stuck_dict_to_high_detail(alert) for alert in alerts]


async def _persisted_episode(db: AsyncSession) -> AnomalyEpisodeRecord:
    carry, high_anomalies = _detect_ongoing_freeze()
    assert len(high_anomalies) == 1, "the freeze must fire exactly one alert"
    written = await persist_stuck_fault_episodes(
        db,
        tenant_id=TENANT,
        greenhouse_id=GH_ID,
        stuck_carry=carry,
        high_anomalies=high_anomalies,
    )
    assert written == 1
    rows = (await db.execute(select(AnomalyEpisodeRecord))).scalars().all()
    assert len(rows) == 1
    return rows[0]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class TestOngoingEpisodeRow:
    async def test_detector_leaves_the_freeze_open(self) -> None:
        """No release sample, so the carry holds an open state and no window."""
        carry, _ = _detect_ongoing_freeze()
        assert carry.completed_faults == []
        assert carry.channels[CHANNEL].last_dt == LAST_FROZEN

    async def test_one_row_is_written_for_the_open_freeze(
        self, db: AsyncSession
    ) -> None:
        """An open freeze that fired is an episode even with no window."""
        episode = await _persisted_episode(db)
        assert episode.channel == CHANNEL
        assert episode.detector == "stuck_fault"

    async def test_is_ongoing_is_true(self, db: AsyncSession) -> None:
        """End of input is not a release: the episode stays open."""
        assert (await _persisted_episode(db)).is_ongoing is True

    async def test_release_ts_is_null(self, db: AsyncSession) -> None:
        """There is no first normal sample yet, so there is no release."""
        assert (await _persisted_episode(db)).release_ts is None

    async def test_end_ts_is_the_last_frozen_sample(
        self, db: AsyncSession
    ) -> None:
        """``end_ts`` tracks the freeze as it continues — it is not the onset.

        Onset is 00:00 and the last frozen sample is 06:35; a row reporting
        the onset here would describe a zero-length freeze.
        """
        episode = await _persisted_episode(db)
        assert _utc(episode.end_ts) == LAST_FROZEN
        assert _utc(episode.end_ts) != _utc(episode.onset_ts)

    async def test_duration_spans_onset_to_last_frozen(
        self, db: AsyncSession
    ) -> None:
        """6 h 35 m of frozen readings, in seconds."""
        assert (await _persisted_episode(db)).duration_seconds == 23_700

    async def test_counts_cover_every_frozen_reading(
        self, db: AsyncSession
    ) -> None:
        """Nothing is missing from this freeze, so the two counts agree."""
        episode = await _persisted_episode(db)
        assert episode.frozen_sample_count == FROZEN_ROWS
        assert episode.raw_slot_count == FROZEN_ROWS

    async def test_detected_ts_lags_the_onset_by_the_gate(
        self, db: AsyncSession
    ) -> None:
        """Detection fired at the row that first satisfied both limbs."""
        episode = await _persisted_episode(db)
        latency_hours = (
            _utc(episode.detected_ts) - _utc(episode.onset_ts)
        ).total_seconds() / 3600.0
        assert latency_hours == pytest.approx(6.0)

    async def test_trigger_value_is_the_frozen_reading(
        self, db: AsyncSession
    ) -> None:
        assert (await _persisted_episode(db)).trigger_value == FROZEN_VALUE

    async def test_rewriting_the_same_open_freeze_updates_in_place(
        self, db: AsyncSession
    ) -> None:
        """A second portion of the same ongoing freeze must not add a row."""
        first = await _persisted_episode(db)
        first_id = first.id
        carry, high_anomalies = _detect_ongoing_freeze()
        await persist_stuck_fault_episodes(
            db,
            tenant_id=TENANT,
            greenhouse_id=GH_ID,
            stuck_carry=carry,
            high_anomalies=high_anomalies,
        )
        rows = (await db.execute(select(AnomalyEpisodeRecord))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == first_id

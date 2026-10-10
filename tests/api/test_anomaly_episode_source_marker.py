"""Item 10: the ``source`` provenance marker on a persisted episode.

Rule (``docs/handoff/task3_4_spec.md`` §2, ``source``): an episode whose
window contains ANY synthetic reading is tagged ``'synthetic:<type>'``, and
when the window mixes several injected types the earliest one by timestamp
wins. ``None`` means every reading in ``[onset_ts, end_ts]`` was real.

Neither native fixture reaches this path — ``native_dec_stuck_fault.csv``
persists four episodes and all four come back with ``source = None``, because
the fixture contains no injected rows at all (pinned in
``tests/api/test_anomaly_episode_persistence.py``). So the rule is exercised
here synthetically: readings are written straight into ``sensor_readings``
with the markers ``synthetic_source()`` produces, and the writer is invoked
with a hand-built carry.

A hand-built ``StuckCarry`` is correct for these tests and wrong for counting
a fixture's episodes: it is built here to hold ONE known window, never to
discover windows in a file. Counting through a bare carry is what manufactures
a phantom co2 episode, and no count in this file comes from one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.api.services import anomaly_episode_writer
from src.api.services.anomaly_episode_writer import persist_stuck_fault_episodes
from src.config import synthetic_source
from src.db.models import AnomalyEpisodeRecord, Base, SensorReading
from src.models.fault_classifier import FaultType
from src.models.stuck_fault_detector import (
    ChannelFreezeState,
    FaultWindow,
    StuckCarry,
)

TENANT = "episode_source"
GH_ID = "gh_episode_source"
CHANNEL = "humidity"
CADENCE_SECONDS = 300.0

ONSET = datetime(2027, 3, 1, 0, 0, tzinfo=timezone.utc)
LAST_FROZEN = ONSET + timedelta(hours=8)
RELEASE = LAST_FROZEN + timedelta(seconds=CADENCE_SECONDS)
DETECTED = ONSET + timedelta(hours=6)

FROZEN_SAMPLES = 97
RAW_SLOTS = 97


@pytest.fixture
async def db() -> AsyncSession:
    """A fresh in-memory database with the full schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


def _stuck_alert() -> UploadHighAnomalyDetail:
    """The stuck_fault alert the writer reads severity/value/detected_ts from."""
    return UploadHighAnomalyDetail(
        score=0.95,
        timestamp=ONSET.isoformat(),
        detected_ts=DETECTED.isoformat(),
        detector="stuck_fault",
        column=CHANNEL,
        severity="urgent",
        value=100.0,
        frozen_hours_at_detection=6.0,
    )


def _released_carry() -> StuckCarry:
    """A carry holding one released freeze that crossed the gate."""
    window = FaultWindow(
        channel=CHANNEL,
        onset_ts=ONSET.isoformat(),
        onset_dt=ONSET,
        release_ts=RELEASE.isoformat(),
        release_dt=RELEASE,
        last_frozen_ts=LAST_FROZEN.isoformat(),
        last_frozen_dt=LAST_FROZEN,
        frozen_sample_count=FROZEN_SAMPLES,
        raw_slot_count=RAW_SLOTS,
        has_fired=True,
    )
    return StuckCarry(completed_faults=[window])


def _open_carry() -> StuckCarry:
    """A carry holding one still-frozen freeze that crossed the gate."""
    state = ChannelFreezeState(
        frozen_value=100.0,
        onset_ts=ONSET.isoformat(),
        onset_dt=ONSET,
        last_ts=LAST_FROZEN.isoformat(),
        last_dt=LAST_FROZEN,
        sample_count=FROZEN_SAMPLES,
        cadence_seconds=CADENCE_SECONDS,
        cadence_branch="pre_onset",
    )
    carry = StuckCarry(channels={CHANNEL: state})
    carry.mark_onset_fired(CHANNEL, state)
    return carry


async def _add_reading(
    db: AsyncSession,
    *,
    timestamp: datetime,
    source: str | None,
    tenant_id: str = TENANT,
    greenhouse_id: str = GH_ID,
) -> None:
    db.add(
        SensorReading(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            timestamp=timestamp,
            temperature=21.0,
            humidity=100.0,
            co2=420.0,
            light=300.0,
            source=source,
        )
    )
    await db.flush()


async def _persist(db: AsyncSession, carry: StuckCarry) -> dict[str, Any]:
    """Run the writer and return the single row it wrote."""
    written = await persist_stuck_fault_episodes(
        db,
        tenant_id=TENANT,
        greenhouse_id=GH_ID,
        stuck_carry=carry,
        high_anomalies=[_stuck_alert()],
    )
    assert written == 1
    rows = (await db.execute(select(AnomalyEpisodeRecord))).scalars().all()
    assert len(rows) == 1
    return {"source": rows[0].source, "is_ongoing": rows[0].is_ongoing}


# =========================================================================
# No synthetic reading in the window
# =========================================================================


class TestRealDetection:
    async def test_all_real_readings_leave_source_null(self, db: AsyncSession) -> None:
        """Every reading real → ``source`` stays NULL, the real-detection case."""
        await _add_reading(db, timestamp=ONSET, source=None)
        await _add_reading(db, timestamp=LAST_FROZEN, source=None)
        assert (await _persist(db, _released_carry()))["source"] is None

    async def test_no_readings_at_all_leaves_source_null(
        self, db: AsyncSession
    ) -> None:
        """A preview-shaped database with no readings is not 'synthetic'."""
        assert (await _persist(db, _released_carry()))["source"] is None


# =========================================================================
# One synthetic type in the window
# =========================================================================


class TestSingleSyntheticType:
    async def test_synthetic_reading_tags_the_episode(
        self, db: AsyncSession
    ) -> None:
        """One injected reading inside the window tags the whole episode."""
        await _add_reading(db, timestamp=ONSET, source=None)
        await _add_reading(
            db,
            timestamp=ONSET + timedelta(hours=1),
            source=synthetic_source("stuck"),
        )
        assert (await _persist(db, _released_carry()))["source"] == "synthetic:stuck"

    async def test_marker_is_read_back_verbatim(self, db: AsyncSession) -> None:
        """The stored string is reused as-is, never rebuilt from a label."""
        marker = synthetic_source("sensor_dropout")
        await _add_reading(db, timestamp=ONSET + timedelta(hours=2), source=marker)
        assert (await _persist(db, _released_carry()))["source"] == marker

    async def test_context_filler_rows_count_as_synthetic(
        self, db: AsyncSession
    ) -> None:
        """Background filler rows carry ``synthetic:context`` and still tag."""
        await _add_reading(
            db, timestamp=ONSET + timedelta(hours=3), source=synthetic_source(None)
        )
        assert (await _persist(db, _released_carry()))["source"] == "synthetic:context"


# =========================================================================
# Several synthetic types — earliest by timestamp wins
# =========================================================================


class TestMixedSyntheticTypes:
    async def test_earliest_type_by_timestamp_wins(self, db: AsyncSession) -> None:
        """With three types in one window, the earliest timestamp decides."""
        await _add_reading(
            db,
            timestamp=ONSET + timedelta(hours=5),
            source=synthetic_source("spike"),
        )
        await _add_reading(
            db,
            timestamp=ONSET + timedelta(hours=1),
            source=synthetic_source("drift"),
        )
        await _add_reading(
            db,
            timestamp=ONSET + timedelta(hours=3),
            source=synthetic_source("stuck"),
        )
        assert (await _persist(db, _released_carry()))["source"] == "synthetic:drift"

    async def test_insertion_order_does_not_decide(self, db: AsyncSession) -> None:
        """The rule is 'earliest by timestamp', not 'first row written'.

        The later-timestamped marker is inserted first here, so a writer that
        took whatever row the database returned first would answer differently.
        """
        await _add_reading(
            db,
            timestamp=ONSET + timedelta(hours=7),
            source=synthetic_source("late_type"),
        )
        await _add_reading(
            db,
            timestamp=ONSET + timedelta(minutes=5),
            source=synthetic_source("early_type"),
        )
        assert (
            await _persist(db, _released_carry())
        )["source"] == "synthetic:early_type"


# =========================================================================
# Window boundaries and tenant scope
# =========================================================================


class TestSourceWindowScope:
    async def test_boundary_readings_are_inside_the_window(
        self, db: AsyncSession
    ) -> None:
        """``[onset_ts, end_ts]`` is inclusive at both ends."""
        await _add_reading(db, timestamp=LAST_FROZEN, source=synthetic_source("edge"))
        assert (await _persist(db, _released_carry()))["source"] == "synthetic:edge"

    async def test_reading_before_onset_is_ignored(self, db: AsyncSession) -> None:
        """A marker from before the freeze does not make the freeze synthetic."""
        await _add_reading(
            db,
            timestamp=ONSET - timedelta(seconds=CADENCE_SECONDS),
            source=synthetic_source("earlier_episode"),
        )
        assert (await _persist(db, _released_carry()))["source"] is None

    async def test_reading_at_release_is_ignored(self, db: AsyncSession) -> None:
        """The window ends at ``end_ts``, so the release sample is outside it.

        This is the discriminating case between the two timestamps: a writer
        that scanned ``[onset_ts, release_ts]`` instead would tag this episode.
        """
        await _add_reading(db, timestamp=RELEASE, source=synthetic_source("after"))
        assert (await _persist(db, _released_carry()))["source"] is None

    async def test_another_tenant_marker_is_ignored(self, db: AsyncSession) -> None:
        """Provenance is scoped to the episode's own tenant."""
        await _add_reading(
            db,
            timestamp=ONSET + timedelta(hours=1),
            source=synthetic_source("other_tenant"),
            tenant_id="some_other_tenant",
        )
        assert (await _persist(db, _released_carry()))["source"] is None

    async def test_another_greenhouse_marker_is_ignored(
        self, db: AsyncSession
    ) -> None:
        """Provenance is scoped to the episode's own greenhouse."""
        await _add_reading(
            db,
            timestamp=ONSET + timedelta(hours=1),
            source=synthetic_source("other_zone"),
            greenhouse_id="gh_elsewhere",
        )
        assert (await _persist(db, _released_carry()))["source"] is None


# =========================================================================
# The rule applies to an ongoing episode too
# =========================================================================


class TestOngoingEpisodeSource:
    async def test_open_freeze_is_tagged_and_left_ongoing(
        self, db: AsyncSession
    ) -> None:
        """An open freeze that fired is persisted ongoing, and still tagged."""
        await _add_reading(
            db,
            timestamp=ONSET + timedelta(hours=2),
            source=synthetic_source("stuck"),
        )
        row = await _persist(db, _open_carry())
        assert row["is_ongoing"] is True
        assert row["source"] == "synthetic:stuck"


# =========================================================================
# Writer validation — the gaps the database cannot catch
# =========================================================================


class TestWriterValidation:
    async def test_no_carry_writes_nothing(self, db: AsyncSession) -> None:
        """No carry means stuck detection never ran: zero rows, no error."""
        written = await persist_stuck_fault_episodes(
            db,
            tenant_id=TENANT,
            greenhouse_id=GH_ID,
            stuck_carry=None,
            high_anomalies=[_stuck_alert()],
        )
        assert written == 0

    async def test_unfired_window_is_not_an_episode(self, db: AsyncSession) -> None:
        """``has_fired=False`` means the dual gate was never crossed."""
        carry = _released_carry()
        carry.completed_faults[0].has_fired = False
        written = await persist_stuck_fault_episodes(
            db,
            tenant_id=TENANT,
            greenhouse_id=GH_ID,
            stuck_carry=carry,
            high_anomalies=[_stuck_alert()],
        )
        assert written == 0
        assert (await db.execute(select(AnomalyEpisodeRecord))).scalars().all() == []

    async def test_missing_sample_count_is_rejected(self, db: AsyncSession) -> None:
        """A null count would persist an unmeasured episode: refuse instead."""
        carry = _released_carry()
        carry.completed_faults[0].frozen_sample_count = None
        with pytest.raises(ValueError, match="missing a sample count"):
            await persist_stuck_fault_episodes(
                db,
                tenant_id=TENANT,
                greenhouse_id=GH_ID,
                stuck_carry=carry,
                high_anomalies=[_stuck_alert()],
            )

    async def test_missing_raw_slot_count_is_rejected(self, db: AsyncSession) -> None:
        """Both counts are required, not just the observed one."""
        carry = _released_carry()
        carry.completed_faults[0].raw_slot_count = None
        with pytest.raises(ValueError, match="missing a sample count"):
            await persist_stuck_fault_episodes(
                db,
                tenant_id=TENANT,
                greenhouse_id=GH_ID,
                stuck_carry=carry,
                high_anomalies=[_stuck_alert()],
            )

    async def test_window_without_a_matching_alert_is_rejected(
        self, db: AsyncSession
    ) -> None:
        """Severity, value and ``detected_ts`` live only on the alert."""
        with pytest.raises(ValueError, match="No stuck_fault alert found"):
            await persist_stuck_fault_episodes(
                db,
                tenant_id=TENANT,
                greenhouse_id=GH_ID,
                stuck_carry=_released_carry(),
                high_anomalies=[],
            )

    async def test_naive_detected_ts_is_read_as_utc(
        self, db: AsyncSession
    ) -> None:
        """An alert timestamp with no offset is UTC, as everywhere else here.

        Statistical rows carry naive ISO strings, so the writer must not
        reject or shift one that reaches it from a stuck alert.
        """
        alert = _stuck_alert().model_copy(
            update={"detected_ts": DETECTED.replace(tzinfo=None).isoformat()}
        )
        written = await persist_stuck_fault_episodes(
            db,
            tenant_id=TENANT,
            greenhouse_id=GH_ID,
            stuck_carry=_released_carry(),
            high_anomalies=[alert],
        )
        assert written == 1
        detected = await db.scalar(select(AnomalyEpisodeRecord.detected_ts))
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=timezone.utc)
        assert detected == DETECTED

    async def test_alert_without_a_detected_ts_falls_back_to_end_ts(
        self, db: AsyncSession
    ) -> None:
        """``detected_ts`` is NOT NULL, so an alert missing one still needs a value.

        The last frozen sample is the latest instant the detection can be
        known at, which makes it the only defensible fallback.
        """
        alert = _stuck_alert().model_copy(update={"detected_ts": None})
        written = await persist_stuck_fault_episodes(
            db,
            tenant_id=TENANT,
            greenhouse_id=GH_ID,
            stuck_carry=_released_carry(),
            high_anomalies=[alert],
        )
        assert written == 1
        detected = await db.scalar(select(AnomalyEpisodeRecord.detected_ts))
        if detected.tzinfo is None:
            detected = detected.replace(tzinfo=timezone.utc)
        assert detected == LAST_FROZEN

    async def test_fault_type_is_validated_in_the_application_layer(
        self, db: AsyncSession
    ) -> None:
        """The database accepts any ``fault_type``; the writer must not.

        ``docs/handoff/task3_4_spec.md`` §5: there is deliberately no DB CHECK
        on this column, so this guard is the only thing standing between a
        renamed fault vocabulary and silently mislabelled episodes. Simulated
        by emptying the vocabulary the writer validates against, since the
        writer's own label is a module constant.
        """
        with patch.object(
            anomaly_episode_writer, "_VALID_FAULT_TYPES", frozenset()
        ):
            with pytest.raises(ValueError, match="Invalid fault_type"):
                await persist_stuck_fault_episodes(
                    db,
                    tenant_id=TENANT,
                    greenhouse_id=GH_ID,
                    stuck_carry=_released_carry(),
                    high_anomalies=[_stuck_alert()],
                )

    def test_the_writers_fault_type_is_in_the_shared_vocabulary(self) -> None:
        """The positive direction: ``stuck_fault`` is a real ``FaultType``."""
        assert "stuck_fault" in {fault.value for fault in FaultType}

    async def test_released_window_wins_over_a_stale_open_state(
        self, db: AsyncSession
    ) -> None:
        """One freeze must not be persisted twice as both closed and open.

        A channel can hold a released window and an open state for the same
        onset at once; the released one is authoritative.
        """
        carry = _released_carry()
        carry.channels[CHANNEL] = ChannelFreezeState(
            frozen_value=100.0,
            onset_ts=ONSET.isoformat(),
            onset_dt=ONSET,
            last_ts=LAST_FROZEN.isoformat(),
            last_dt=LAST_FROZEN,
            sample_count=FROZEN_SAMPLES,
            cadence_seconds=CADENCE_SECONDS,
            cadence_branch="pre_onset",
        )
        carry.mark_onset_fired(CHANNEL, carry.channels[CHANNEL])
        row = await _persist(db, carry)
        assert row["is_ongoing"] is False

"""Harness for the Task 5 cross-channel ``is_recovery`` tagging tests.

Builds the two things the tagging reads, and nothing else: released
stuck-fault rows in ``anomaly_episodes`` (``AnomalyEpisodeRecord``), and
flagged ``sensor_readings`` rows the query-time grouper turns into
episodes.  Both are written directly rather than produced by an upload,
because the cases under test are window boundaries measured in seconds
around a release — a real upload cannot be steered to land an episode
start exactly ``EPISODE_GAP_SECONDS`` before one.  Fixture A covers the
pipeline-produced case instead, in ``test_recovery_tag_fixture_a.py``.

Tests call ``fetch_anomaly_episodes`` (``anomaly_episode_service``), the
function ``GET /api/anomalies/episodes`` delegates to, so the whole
query-time path — grouping, stuck-fault augmentation, ranking, capping,
then tagging — runs exactly as the endpoint runs it.

Consumed by ``tests/api/test_recovery_tag_window.py`` and
``tests/api/test_recovery_tag_cross_upload.py``.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.api.services.anomaly_episode_service import fetch_anomaly_episodes
from src.config import ANOMALY_GROUP_SAMPLE_IDS, CANONICAL_ANOMALY_THRESHOLD
from src.db.models import AnomalyEpisodeRecord, Base, SensorReading

TENANT = "tenant_recovery_tag"
GH_ID = "gh_recovery_tag"

STUCK_DETECTOR = "stuck_fault"
STATISTICAL_DETECTOR = "statistical"

# The faulted channel and a downstream one.  Fixture A's real pairing is a
# humidity freeze followed by pipe/heating excursions; "temperature" stands
# in for the downstream channel here because it has a configured band, so
# the grouper assigns it a stable direction and a run never fractures.
FAULT_CHANNEL = "humidity"
OTHER_CHANNEL = "temperature"

# Out-of-band values, so every row in a run shares one direction.
FROZEN_HUMIDITY = 30.0
EXCURSION_TEMPERATURE = 45.0

# Readings are future-dated relative to wall clock, as everywhere in this
# repo's fixtures — a window end is never "now"
# (project_future_dated_readings).
RELEASE = datetime(2026, 12, 25, 8, 55, tzinfo=timezone.utc)
SAMPLE_SPACING = timedelta(minutes=5)

# Wide enough that no episode is clipped by the queried window.
WINDOW_START = RELEASE - timedelta(days=2)
WINDOW_END = RELEASE + timedelta(days=2)

Episode = dict[str, Any]


@contextlib.asynccontextmanager
async def recovery_tag_engine() -> AsyncIterator[AsyncEngine]:
    """An empty in-memory database with the full schema.

    Yields the ENGINE rather than a session so a test can open more than
    one — the cross-upload case needs the release and the hit it tags to
    be written by different sessions, sharing nothing but the database.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


@contextlib.asynccontextmanager
async def session_for(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One session on *engine*, closed on exit."""
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session


@contextlib.asynccontextmanager
async def recovery_tag_db() -> AsyncIterator[AsyncSession]:
    """One session on a fresh empty database — the common single-writer case."""
    async with recovery_tag_engine() as engine:
        async with session_for(engine) as session:
            yield session


async def add_released_fault(
    session: AsyncSession,
    *,
    channel: str = FAULT_CHANNEL,
    release: datetime | None = RELEASE,
) -> None:
    """Record one stuck fault that froze for a day and then released.

    ``release=None`` writes the fault as still ongoing, which is the shape
    ``ck_anomaly_episodes_release_ts_null_while_ongoing`` requires for a
    NULL ``release_ts``.
    """
    anchor = release if release is not None else RELEASE
    onset = anchor - timedelta(days=1)
    last_frozen = anchor - SAMPLE_SPACING
    session.add(
        AnomalyEpisodeRecord(
            tenant_id=TENANT,
            greenhouse_id=GH_ID,
            channel=channel,
            detector=STUCK_DETECTOR,
            fault_type=STUCK_DETECTOR,
            onset_ts=onset,
            end_ts=last_frozen,
            detected_ts=onset + timedelta(hours=6),
            release_ts=release,
            duration_seconds=int((last_frozen - onset).total_seconds()),
            frozen_sample_count=288,
            raw_slot_count=288,
            severity="urgent",
            trigger_value=FROZEN_HUMIDITY,
            is_ongoing=release is None,
        )
    )
    await session.commit()


async def add_flagged_run(
    session: AsyncSession,
    *,
    first_ts: datetime,
    sample_count: int,
    channel: str = OTHER_CHANNEL,
    detector: str = STATISTICAL_DETECTOR,
    score: float = 0.9,
) -> None:
    """Add a contiguous run of flagged readings on *channel*.

    Samples sit ``SAMPLE_SPACING`` apart, inside the query-time grouper's
    contiguity gap, so the run reads as ONE episode starting at *first_ts*.
    """
    value = FROZEN_HUMIDITY if channel == FAULT_CHANNEL else EXCURSION_TEMPERATURE
    for index in range(sample_count):
        session.add(
            SensorReading(
                tenant_id=TENANT,
                greenhouse_id=GH_ID,
                timestamp=first_ts + index * SAMPLE_SPACING,
                temperature=value if channel != FAULT_CHANNEL else 22.0,
                humidity=value if channel == FAULT_CHANNEL else 60.0,
                co2=400.0,
                light=500.0,
                anomaly_score=score,
                trigger_channel=channel,
                trigger_detector=detector,
                detected_severity="warning",
                trigger_value=value,
            )
        )
    await session.commit()


async def query_episodes(session: AsyncSession) -> list[Episode]:
    """Every episode the endpoint would return, newest-starting last."""
    episodes = await fetch_anomaly_episodes(
        session,
        tenant_id=TENANT,
        greenhouse_id=GH_ID,
        start=WINDOW_START,
        end=WINDOW_END,
        threshold=CANONICAL_ANOMALY_THRESHOLD,
        sample_limit=ANOMALY_GROUP_SAMPLE_IDS,
        incidents=[],
    )
    return sorted(episodes, key=lambda ep: ep["start_ts"])


async def single_episode(session: AsyncSession) -> Episode:
    """The one episode in the database — asserted, never assumed.

    A boundary test that silently read episode zero of several would pin
    whichever one sorted first rather than the one it set up.
    """
    episodes = await query_episodes(session)
    assert len(episodes) == 1, [
        (ep["channel"], ep["start_ts"], ep["sample_count"]) for ep in episodes
    ]
    return episodes[0]

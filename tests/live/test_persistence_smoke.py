"""Smoke tests for PersistenceBatcher (Phase 3B).

Coverage
--------
- Batched flush writes events to an in-memory SQLite session.
- Duplicate (tenant, greenhouse, timestamp) does NOT error (ON CONFLICT DO NOTHING).
- NaN channel is sanitized / replaced with 0.0 — row inserts successfully.
- Buffer drop-oldest cap: oldest events are discarded when cap is exceeded.
- close() flushes remaining buffered rows.
"""

from __future__ import annotations

import asyncio
import datetime
from datetime import timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.score_transform import canonical as _canonical_score
from src.config import SCORE_SCALE_CALIBRATION
from src.db.models import Base, SensorReading
from src.live.events import LiveReadingEvent
from src.live.persistence import PersistenceBatcher, _BUFFER_CAP

# ADR-005: live scores are transformed at write time.
_LIVE_P, _LIVE_HI = SCORE_SCALE_CALIBRATION["live"]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TENANT = "persist_tenant"
_GH = "gh_persist"


def _ts(minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 7, 18, 12, minute, 0, tzinfo=timezone.utc)


def _make_event(
    minute: int = 0,
    tenant: str = _TENANT,
    greenhouse: str = _GH,
    anomaly_score: float | None = None,
) -> LiveReadingEvent:
    return LiveReadingEvent(
        seq="",
        timestamp=_ts(minute),
        tenant_id=tenant,
        greenhouse_id=greenhouse,
        channels={
            "temperature": 22.0 + minute,
            "humidity": 60.0,
            "co2": 400.0,
            "light": 500.0,
        },
        sources={"temperature": "src1"},
        anomaly_score=anomaly_score,
    )


@pytest_asyncio.fixture
async def db_engine():
    """In-memory SQLite engine with schema created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine) -> async_sessionmaker:
    """Async session factory bound to the test engine."""
    return async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False
    )


@pytest_asyncio.fixture
async def batcher(session_factory) -> AsyncGenerator[PersistenceBatcher, None]:
    """PersistenceBatcher with a tiny flush interval and the test session."""
    b = PersistenceBatcher(flush_interval=0.05, session_factory=session_factory)
    yield b
    # Ensure clean state; close() does a final flush.
    await b.close()


async def _count(session_factory) -> int:
    """Count SensorReading rows for the test scope."""
    async with session_factory() as session:
        result = await session.execute(
            select(func.count(SensorReading.id)).where(
                SensorReading.tenant_id == _TENANT
            )
        )
        return result.scalar() or 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_writes_rows_to_db(batcher: PersistenceBatcher, session_factory) -> None:
    """Events added to the batcher are written to the DB on flush."""
    await batcher.add(_make_event(0))
    await batcher.add(_make_event(1))
    await batcher._flush()

    assert await _count(session_factory) == 2


@pytest.mark.asyncio
async def test_duplicate_row_does_not_error(batcher: PersistenceBatcher, session_factory) -> None:
    """Inserting the same (tenant, greenhouse, timestamp) twice is a no-op."""
    event = _make_event(0)
    await batcher.add(event)
    await batcher._flush()

    await batcher.add(event)  # exact duplicate
    await batcher._flush()  # must not raise

    assert await _count(session_factory) == 1


@pytest.mark.asyncio
async def test_nan_channel_sanitized(batcher: PersistenceBatcher, session_factory) -> None:
    """NaN in a present channel is replaced with 0.0; complete rows insert without error."""
    # Use model_construct to bypass Pydantic validation and inject NaN.
    # All four canonical channels must be present; NaN is sanitized, not treated as missing.
    event = LiveReadingEvent.model_construct(
        seq="",
        timestamp=_ts(5),
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        channels={
            "temperature": float("nan"),
            "humidity": 60.0,
            "co2": 400.0,
            "light": 500.0,
        },
        sources={},
        anomaly_score=None,
    )
    await batcher.add(event)
    await batcher._flush()

    async with session_factory() as session:
        row = await session.execute(
            select(SensorReading).where(
                SensorReading.tenant_id == _TENANT,
                SensorReading.timestamp == _ts(5),
            )
        )
        reading: SensorReading | None = row.scalars().first()

    assert reading is not None, "Expected a row to be inserted despite NaN channel"
    assert reading.temperature == 0.0, "NaN temperature should default to 0.0"
    assert reading.humidity == 60.0


@pytest.mark.asyncio
async def test_anomaly_score_persisted(batcher: PersistenceBatcher, session_factory) -> None:
    """anomaly_score is written to the DB row when provided."""
    event = _make_event(10, anomaly_score=0.92)
    await batcher.add(event)
    await batcher._flush()

    async with session_factory() as session:
        row = await session.execute(
            select(SensorReading).where(SensorReading.tenant_id == _TENANT)
        )
        reading: SensorReading | None = row.scalars().first()

    assert reading is not None
    # ADR-005: anomaly_score now stores the canonical score; raw is in anomaly_score_raw.
    expected_canonical = _canonical_score(0.92, _LIVE_P, _LIVE_HI)
    assert expected_canonical is not None
    assert abs(reading.anomaly_score - expected_canonical) < 1e-6
    assert abs(reading.anomaly_score_raw - 0.92) < 1e-6
    assert reading.score_scale == "live"


@pytest.mark.asyncio
async def test_close_flushes_remaining_events(
    batcher: PersistenceBatcher, session_factory
) -> None:
    """close() flushes any buffered events without waiting for the interval."""
    await batcher.add(_make_event(20))
    await batcher.add(_make_event(21))
    await batcher.close()

    assert await _count(session_factory) == 2


@pytest.mark.asyncio
async def test_buffer_cap_drops_oldest(session_factory) -> None:
    """When buffer cap is exceeded, the oldest events are dropped."""
    b = PersistenceBatcher(flush_interval=9999.0, session_factory=session_factory)

    # Fill up to cap + 2 (two oldest should be dropped)
    overflow = 2
    for i in range(_BUFFER_CAP + overflow):
        await b.add(_make_event(0))  # same ts is fine — tests cap, not dedup

    # Buffer should contain exactly _BUFFER_CAP items.
    assert len(b._buffer) == _BUFFER_CAP
    assert b._dropped_count == overflow
    await b.close()


@pytest.mark.asyncio
async def test_run_loop_flushes_on_interval(batcher: PersistenceBatcher, session_factory) -> None:
    """The background run() loop flushes events periodically."""
    await batcher.add(_make_event(30))

    task = asyncio.create_task(batcher.run())
    # Allow the flush interval (0.05 s) to elapse with some headroom.
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # At least the event added before start should have been flushed.
    count = await _count(session_factory)
    assert count >= 1

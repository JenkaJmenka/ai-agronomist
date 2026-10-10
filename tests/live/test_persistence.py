"""Comprehensive tests for PersistenceBatcher.

Covers: flush batching; on-conflict-do-nothing on duplicate; NaN sanitization;
flush failure retains rows; buffer hard-cap drop-oldest; no session held
between flushes.
"""

from __future__ import annotations

import asyncio
import datetime
from datetime import timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.score_transform import canonical as _canonical_score
from src.config import SCORE_SCALE_CALIBRATION
from src.db.models import Base, SensorReading
from src.live.events import LiveReadingEvent
from src.live.persistence import PersistenceBatcher, _BUFFER_CAP, _CANONICAL_CHANNELS, _event_to_row

# ADR-005: live scores are transformed at write time.
_LIVE_P, _LIVE_HI = SCORE_SCALE_CALIBRATION["live"]


def _expected_live_canonical(raw: float) -> float:
    """Compute the expected canonical score for a given live raw score."""
    result = _canonical_score(raw, _LIVE_P, _LIVE_HI)
    assert result is not None, f"canonical({raw!r}, {_LIVE_P!r}, {_LIVE_HI!r}) returned None"
    return result


# ---------------------------------------------------------------------------
# Constants / Helpers
# ---------------------------------------------------------------------------

_TENANT = "persist_tenant"
_GH = "gh_persist"


def _ts(minute: int = 0, second: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 7, 18, 12, minute, second, tzinfo=timezone.utc)


def _make_event(
    minute: int = 0,
    second: int = 0,
    tenant: str = _TENANT,
    greenhouse: str = _GH,
    anomaly_score: float | None = None,
    temperature: float = 22.0,
) -> LiveReadingEvent:
    return LiveReadingEvent(
        seq="",
        timestamp=_ts(minute, second),
        tenant_id=tenant,
        greenhouse_id=greenhouse,
        channels={
            "temperature": temperature + minute,
            "humidity": 60.0,
            "co2": 400.0,
            "light": 500.0,
        },
        sources={"temperature": "src1"},
        anomaly_score=anomaly_score,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine) -> async_sessionmaker:
    return async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False,
    )


@pytest_asyncio.fixture
async def batcher(session_factory) -> AsyncGenerator[PersistenceBatcher, None]:
    b = PersistenceBatcher(flush_interval=0.05, session_factory=session_factory)
    yield b
    await b.close()


async def _count(session_factory, tenant: str = _TENANT) -> int:
    async with session_factory() as session:
        result = await session.execute(
            select(func.count(SensorReading.id)).where(
                SensorReading.tenant_id == tenant
            )
        )
        return result.scalar() or 0


# ---------------------------------------------------------------------------
# _event_to_row unit tests
# ---------------------------------------------------------------------------


class TestEventToRow:

    def test_basic_conversion(self) -> None:
        event = _make_event(minute=5)
        row = _event_to_row(event)
        assert row["tenant_id"] == _TENANT
        assert row["greenhouse_id"] == _GH
        assert row["timestamp"] == _ts(5)
        assert abs(row["temperature"] - 27.0) < 1e-6
        assert row["source"] == "live"

    def test_incomplete_event_returns_none(self) -> None:
        """Events missing any canonical channel return None (skip policy)."""
        event = LiveReadingEvent(
            seq="", timestamp=_ts(0), tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"temperature": 22.0},  # missing humidity, co2, light
            sources={},
        )
        assert _event_to_row(event) is None

    def test_nan_channel_sanitized_to_zero_when_complete(self) -> None:
        """NaN in a channel that is present is replaced with 0.0; complete rows are kept."""
        event = LiveReadingEvent.model_construct(
            seq="", timestamp=_ts(0), tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": float("nan"),
                "humidity": 60.0,
                "co2": 400.0,
                "light": 500.0,
            },
            sources={}, anomaly_score=None,
        )
        row = _event_to_row(event)
        assert row is not None
        assert row["temperature"] == 0.0
        assert row["humidity"] == 60.0

    def test_partial_nan_event_returns_none_when_incomplete(self) -> None:
        """NaN channel + missing channels → incomplete → None."""
        event = LiveReadingEvent.model_construct(
            seq="", timestamp=_ts(0), tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"temperature": float("nan"), "humidity": 60.0},
            sources={}, anomaly_score=None,
        )
        assert _event_to_row(event) is None

    def test_anomaly_score_included(self) -> None:
        raw = 0.85
        event = _make_event(anomaly_score=raw)
        row = _event_to_row(event)
        # ADR-005: stored score is canonical, raw is preserved in anomaly_score_raw.
        expected_canonical = _expected_live_canonical(raw)
        assert abs(row["anomaly_score"] - expected_canonical) < 1e-6
        assert abs(row["anomaly_score_raw"] - raw) < 1e-6
        assert row["score_scale"] == "live"

    def test_anomaly_score_none(self) -> None:
        event = _make_event(anomaly_score=None)
        row = _event_to_row(event)
        assert row["anomaly_score"] is None


# ---------------------------------------------------------------------------
# Flush batching
# ---------------------------------------------------------------------------


class TestFlushBatching:

    @pytest.mark.asyncio
    async def test_flush_writes_multiple_rows(self, batcher: PersistenceBatcher, session_factory) -> None:
        await batcher.add(_make_event(0))
        await batcher.add(_make_event(1))
        await batcher.add(_make_event(2))
        await batcher._flush()
        assert await _count(session_factory) == 3

    @pytest.mark.asyncio
    async def test_flush_empty_buffer_noop(self, batcher: PersistenceBatcher, session_factory) -> None:
        await batcher._flush()
        assert await _count(session_factory) == 0

    @pytest.mark.asyncio
    async def test_run_loop_flushes_on_interval(self, batcher: PersistenceBatcher, session_factory) -> None:
        await batcher.add(_make_event(30))
        task = asyncio.create_task(batcher.run())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert await _count(session_factory) >= 1

    @pytest.mark.asyncio
    async def test_close_flushes_remaining(self, session_factory) -> None:
        b = PersistenceBatcher(flush_interval=9999.0, session_factory=session_factory)
        await b.add(_make_event(40))
        await b.add(_make_event(41))
        await b.close()
        assert await _count(session_factory) == 2


# ---------------------------------------------------------------------------
# Duplicate handling (ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------


class TestDuplicateHandling:

    @pytest.mark.asyncio
    async def test_duplicate_row_ignored(self, batcher: PersistenceBatcher, session_factory) -> None:
        event = _make_event(0)
        await batcher.add(event)
        await batcher._flush()
        await batcher.add(event)
        await batcher._flush()
        assert await _count(session_factory) == 1

    @pytest.mark.asyncio
    async def test_different_timestamp_not_duplicate(self, batcher: PersistenceBatcher, session_factory) -> None:
        await batcher.add(_make_event(0, second=0))
        await batcher.add(_make_event(0, second=1))
        await batcher._flush()
        assert await _count(session_factory) == 2


# ---------------------------------------------------------------------------
# NaN sanitization
# ---------------------------------------------------------------------------


class TestNanSanitization:

    @pytest.mark.asyncio
    async def test_nan_channel_written_as_zero_when_all_channels_present(
        self, batcher: PersistenceBatcher, session_factory
    ) -> None:
        """NaN in a present channel becomes 0.0; complete rows are still written."""
        event = LiveReadingEvent.model_construct(
            seq="", timestamp=_ts(5), tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": float("nan"),
                "humidity": 60.0,
                "co2": 400.0,
                "light": 500.0,
            },
            sources={}, anomaly_score=None,
        )
        await batcher.add(event)
        await batcher._flush()

        async with session_factory() as session:
            row = (await session.execute(
                select(SensorReading).where(SensorReading.tenant_id == _TENANT)
            )).scalars().first()

        assert row is not None
        assert row.temperature == 0.0
        assert row.humidity == 60.0

    @pytest.mark.asyncio
    async def test_inf_channel_written_as_zero_when_all_channels_present(
        self, batcher: PersistenceBatcher, session_factory
    ) -> None:
        """Inf in a present channel becomes 0.0; complete rows write successfully."""
        event = LiveReadingEvent.model_construct(
            seq="", timestamp=_ts(6), tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": float("inf"),
                "humidity": 60.0,
                "co2": 400.0,
                "light": 500.0,
            },
            sources={}, anomaly_score=None,
        )
        await batcher.add(event)
        await batcher._flush()

        async with session_factory() as session:
            row = (await session.execute(
                select(SensorReading).where(SensorReading.tenant_id == _TENANT)
            )).scalars().first()

        assert row is not None
        assert row.temperature == 0.0

    @pytest.mark.asyncio
    async def test_incomplete_event_skipped_not_written(
        self, batcher: PersistenceBatcher, session_factory
    ) -> None:
        """Incomplete events (missing any canonical channel) are skipped — not written."""
        event = LiveReadingEvent.model_construct(
            seq="", timestamp=_ts(7), tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"temperature": float("nan"), "humidity": 60.0},  # missing co2 + light
            sources={}, anomaly_score=None,
        )
        await batcher.add(event)
        await batcher._flush()

        assert batcher._skipped_incomplete_count == 1
        assert await _count(session_factory) == 0


# ---------------------------------------------------------------------------
# Flush failure retains rows
# ---------------------------------------------------------------------------


class TestFlushFailure:

    @pytest.mark.asyncio
    async def test_failure_retains_rows_for_retry(self) -> None:
        """On flush error, rows are returned to buffer for next cycle."""

        class _FailingSession:
            """Async context manager that raises on enter."""
            async def __aenter__(self):
                raise RuntimeError("DB down")
            async def __aexit__(self, *args):
                pass

        def failing_factory():
            return _FailingSession()

        b = PersistenceBatcher(flush_interval=0.05, session_factory=failing_factory)
        await b.add(_make_event(0))
        await b.add(_make_event(1))

        await b._flush()  # should fail but retain rows
        assert len(b._buffer) == 2
        await b.close()

    @pytest.mark.asyncio
    async def test_failure_then_success(self, session_factory) -> None:
        """After a failed flush, rows flush successfully on next cycle."""

        class _FailingSession:
            async def __aenter__(self):
                raise RuntimeError("DB down")
            async def __aexit__(self, *args):
                pass

        call_count = [0]

        def tracking_factory():
            call_count[0] += 1
            if call_count[0] == 1:
                return _FailingSession()
            return session_factory()

        b = PersistenceBatcher(flush_interval=0.05, session_factory=tracking_factory)
        await b.add(_make_event(10))

        await b._flush()  # fails
        assert len(b._buffer) == 1

        await b._flush()  # succeeds (second call uses real factory)
        assert len(b._buffer) == 0
        assert await _count(session_factory) == 1
        await b.close()


# ---------------------------------------------------------------------------
# Buffer hard-cap drop-oldest
# ---------------------------------------------------------------------------


class TestBufferCap:

    @pytest.mark.asyncio
    async def test_cap_drops_oldest(self, session_factory) -> None:
        b = PersistenceBatcher(flush_interval=9999.0, session_factory=session_factory)
        overflow = 5
        for i in range(_BUFFER_CAP + overflow):
            await b.add(_make_event(0))
        assert len(b._buffer) == _BUFFER_CAP
        assert b._dropped_count == overflow
        await b.close()

    @pytest.mark.asyncio
    async def test_cap_preserves_newest(self, session_factory) -> None:
        """After overflow, the buffer contains the newest events."""
        b = PersistenceBatcher(flush_interval=9999.0, session_factory=session_factory)
        # Add cap + 2 events (all same timestamp — tests cap, not uniqueness)
        for i in range(_BUFFER_CAP + 2):
            await b.add(_make_event(minute=0, second=0))

        # The oldest 2 should have been dropped
        assert len(b._buffer) == _BUFFER_CAP
        assert b._dropped_count == 2
        await b.close()


# ---------------------------------------------------------------------------
# No session held between flushes
# ---------------------------------------------------------------------------


class TestSessionLifecycle:

    @pytest.mark.asyncio
    async def test_session_opened_per_flush(self, db_engine) -> None:
        """Each flush opens and closes its own session — no session held between."""
        sessions_opened: list[int] = [0]

        real_factory = async_sessionmaker(
            bind=db_engine, class_=AsyncSession, expire_on_commit=False,
        )

        def tracking_factory():
            sessions_opened[0] += 1
            return real_factory()

        b = PersistenceBatcher(flush_interval=0.05, session_factory=tracking_factory)

        await b.add(_make_event(0))
        await b._flush()
        flush1_count = sessions_opened[0]

        await b.add(_make_event(1))
        await b._flush()
        flush2_count = sessions_opened[0]

        assert flush2_count > flush1_count
        await b.close()


# ---------------------------------------------------------------------------
# Background task lifecycle
# ---------------------------------------------------------------------------


class TestBackgroundTask:

    @pytest.mark.asyncio
    async def test_start_background_task(self, session_factory) -> None:
        b = PersistenceBatcher(flush_interval=0.05, session_factory=session_factory)
        await b.add(_make_event(50))
        b.start_background_task()
        await asyncio.sleep(0.15)
        await b.close()
        assert await _count(session_factory) >= 1

    @pytest.mark.asyncio
    async def test_close_cancels_background_task(self, session_factory) -> None:
        b = PersistenceBatcher(flush_interval=0.05, session_factory=session_factory)
        b.start_background_task()
        assert b._task is not None
        await b.close()
        assert b._task is None

    @pytest.mark.asyncio
    async def test_run_cancel_does_final_flush(self, session_factory) -> None:
        """Cancellation of run() triggers a final flush."""
        b = PersistenceBatcher(flush_interval=9999.0, session_factory=session_factory)
        await b.add(_make_event(55))

        task = asyncio.create_task(b.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert await _count(session_factory) >= 1


class TestPersistenceSuppress:
    @pytest.fixture(autouse=True)
    def _reset_scope_gate(self):
        from src.live.scope_control import clear_all_suppressed_scopes

        clear_all_suppressed_scopes()
        yield
        clear_all_suppressed_scopes()

    @pytest.mark.asyncio
    async def test_suppress_drops_buffered_and_blocks_add(
        self, session_factory
    ) -> None:
        b = PersistenceBatcher(flush_interval=9999.0, session_factory=session_factory)
        await b.add(_make_event(1))
        await b.add(_make_event(2, tenant="other"))
        b.suppress_scope(_TENANT, _GH)
        assert b.is_scope_suppressed(_TENANT, _GH)
        assert len(b._buffer) == 1
        assert b._buffer[0].tenant_id == "other"

        await b.add(_make_event(3))
        assert all(ev.tenant_id != _TENANT for ev in b._buffer)

        await b._flush()
        assert await _count(session_factory, _TENANT) == 0
        assert await _count(session_factory, "other") == 1

    @pytest.mark.asyncio
    async def test_resume_allows_persist_again(self, session_factory) -> None:
        b = PersistenceBatcher(flush_interval=9999.0, session_factory=session_factory)
        b.suppress_scope(_TENANT, _GH)
        await b.add(_make_event(1))
        assert len(b._buffer) == 0

        b.resume_scope(_TENANT, _GH)
        await b.add(_make_event(2))
        await b._flush()
        assert await _count(session_factory) == 1

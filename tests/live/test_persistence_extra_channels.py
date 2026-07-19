"""Tests for persistence of events with extra channels (soil_moisture, root_zone_temp, vpd).

Covers: events with extra channels flush without error; extra channels are NOT
persisted (only the 4 canonical channels + anomaly_score are stored); incomplete
events still skipped correctly when extra channels are present.
"""

from __future__ import annotations

import asyncio
import datetime
from datetime import timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base, SensorReading
from src.live.events import LiveReadingEvent
from src.live.persistence import PersistenceBatcher, _event_to_row


# ---------------------------------------------------------------------------
# Constants / Helpers
# ---------------------------------------------------------------------------

_TENANT = "persist_extra"
_GH = "gh_extra"


def _ts(second: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 7, 18, 12, 0, second, tzinfo=timezone.utc)


def _make_event_with_extras(
    second: int = 0,
    include_vpd: bool = True,
    include_soil: bool = True,
    include_rzt: bool = True,
    anomaly_score: float | None = None,
) -> LiveReadingEvent:
    """Build a reading with the 4 canonical channels + extra channels."""
    channels = {
        "temperature": 25.0,
        "humidity": 65.0,
        "co2": 400.0,
        "light": 500.0,
    }
    sources = {
        "temperature": "src1",
        "humidity": "src1",
        "co2": "src2",
        "light": "src1",
    }
    if include_vpd:
        channels["vpd"] = 1.27
        sources["vpd"] = "derived"
    if include_soil:
        channels["soil_moisture"] = 72.0
        sources["soil_moisture"] = "src3"
    if include_rzt:
        channels["root_zone_temp"] = 21.5
        sources["root_zone_temp"] = "src3"

    return LiveReadingEvent(
        seq="",
        timestamp=_ts(second),
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        channels=channels,
        sources=sources,
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
async def batcher(session_factory):
    b = PersistenceBatcher(flush_interval=0.05, session_factory=session_factory)
    yield b
    await b.close()


# ---------------------------------------------------------------------------
# _event_to_row — extra channels ignored
# ---------------------------------------------------------------------------


class TestEventToRowExtraChannels:

    def test_row_includes_canonical_channels_only(self) -> None:
        """Extra channels (vpd, soil_moisture, root_zone_temp) are not in the row dict."""
        event = _make_event_with_extras()
        row = _event_to_row(event)
        assert row is not None
        assert "temperature" in row
        assert "humidity" in row
        assert "co2" in row
        assert "light" in row
        # Extra channels should NOT be in the row
        assert "vpd" not in row
        assert "soil_moisture" not in row
        assert "root_zone_temp" not in row

    def test_canonical_values_correct(self) -> None:
        """The 4 canonical channel values are correctly extracted."""
        event = _make_event_with_extras()
        row = _event_to_row(event)
        assert row is not None
        assert abs(row["temperature"] - 25.0) < 1e-6
        assert abs(row["humidity"] - 65.0) < 1e-6
        assert abs(row["co2"] - 400.0) < 1e-6
        assert abs(row["light"] - 500.0) < 1e-6

    def test_completeness_check_ignores_extras(self) -> None:
        """Event is complete if 4 canonical channels present, regardless of extras."""
        event = _make_event_with_extras(include_vpd=True, include_soil=True, include_rzt=True)
        row = _event_to_row(event)
        assert row is not None

    def test_missing_canonical_still_rejected(self) -> None:
        """Event with extras but missing a canonical channel is still rejected."""
        event = LiveReadingEvent(
            seq="",
            timestamp=_ts(0),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": 25.0,
                "humidity": 65.0,
                # co2 missing!
                "light": 500.0,
                "vpd": 1.27,
                "soil_moisture": 72.0,
                "root_zone_temp": 21.5,
            },
            sources={},
        )
        row = _event_to_row(event)
        assert row is None


# ---------------------------------------------------------------------------
# Flush with extra channels — no error
# ---------------------------------------------------------------------------


class TestFlushWithExtraChannels:

    @pytest.mark.asyncio
    async def test_flush_succeeds_with_extra_channels(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """Events with extra channels flush without error."""
        await batcher.add(_make_event_with_extras(second=0))
        await batcher.add(_make_event_with_extras(second=1))
        await batcher._flush()

        # Verify rows written
        async with session_factory() as session:
            rows = (await session.execute(
                select(SensorReading).where(SensorReading.tenant_id == _TENANT)
            )).scalars().all()

        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_persisted_row_has_only_canonical_columns(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """Persisted row contains only the canonical 4 channels, not extras."""
        await batcher.add(_make_event_with_extras(second=10, anomaly_score=0.5))
        await batcher._flush()

        async with session_factory() as session:
            row = (await session.execute(
                select(SensorReading).where(SensorReading.tenant_id == _TENANT)
            )).scalars().first()

        assert row is not None
        assert abs(row.temperature - 25.0) < 1e-6
        assert abs(row.humidity - 65.0) < 1e-6
        assert abs(row.co2 - 400.0) < 1e-6
        assert abs(row.light - 500.0) < 1e-6
        assert abs(row.anomaly_score - 0.5) < 1e-6

        # SensorReading model should NOT have vpd/soil_moisture/root_zone_temp columns
        assert not hasattr(row, "vpd")
        assert not hasattr(row, "soil_moisture")
        assert not hasattr(row, "root_zone_temp")

    @pytest.mark.asyncio
    async def test_event_with_only_extras_rejected(
        self, batcher: PersistenceBatcher, session_factory,
    ) -> None:
        """An event with only extra channels (no canonical) is rejected."""
        event = LiveReadingEvent(
            seq="",
            timestamp=_ts(20),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "vpd": 1.27,
                "soil_moisture": 72.0,
                "root_zone_temp": 21.5,
            },
            sources={},
        )
        await batcher.add(event)
        await batcher._flush()

        async with session_factory() as session:
            count = (await session.execute(
                select(SensorReading).where(SensorReading.tenant_id == _TENANT)
            )).scalars().all()

        assert len(count) == 0
        assert batcher._skipped_incomplete_count >= 1

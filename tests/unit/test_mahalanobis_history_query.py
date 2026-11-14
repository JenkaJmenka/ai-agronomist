"""Test _query_physical_history against an in-memory SQLite session.

C.  Rows with source "synthetic:spike" are excluded, rows with NULL source
    are kept, rows at or after `before` are excluded, rows older than
    DIURNAL_PROFILE_HISTORY_DAYS before `before` are excluded, and the
    result has shape (n, 4).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.comprehensive_pipeline import _query_physical_history
from src.config import DIURNAL_PROFILE_HISTORY_DAYS
from src.db.models import Base, SensorReading

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def engine(event_loop):
    eng = event_loop.run_until_complete(
        _create_engine()
    )
    yield eng
    event_loop.run_until_complete(eng.dispose())


async def _create_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return eng


@pytest.fixture(scope="module")
def session_factory(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="module")
def _seed_data(event_loop, session_factory):
    """Seed the database with test rows once for all tests in this module."""
    event_loop.run_until_complete(_do_seed(session_factory))


async def _do_seed(factory):
    # "before" anchor for all tests: 2026-07-10 12:00 UTC
    anchor = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

    async with factory() as session:
        readings = []

        # 1. Normal rows (NULL source) within the window — should be kept
        for i in range(10):
            readings.append(SensorReading(
                tenant_id="t1",
                greenhouse_id="g1",
                timestamp=anchor - timedelta(hours=i + 1),
                temperature=22.0 + i * 0.1,
                humidity=60.0 + i * 0.5,
                co2=400.0 + i,
                light=500.0 + i * 10,
                source=None,
            ))

        # 2. Synthetic row (should be excluded)
        readings.append(SensorReading(
            tenant_id="t1",
            greenhouse_id="g1",
            timestamp=anchor - timedelta(hours=11),
            temperature=99.0,
            humidity=99.0,
            co2=9999.0,
            light=1999.0,
            source="synthetic:spike",
        ))

        # 3. Row exactly AT anchor (should be excluded by `< anchor`)
        readings.append(SensorReading(
            tenant_id="t1",
            greenhouse_id="g1",
            timestamp=anchor,
            temperature=25.0,
            humidity=65.0,
            co2=450.0,
            light=600.0,
            source=None,
        ))

        # 4. Row AFTER anchor (should be excluded)
        readings.append(SensorReading(
            tenant_id="t1",
            greenhouse_id="g1",
            timestamp=anchor + timedelta(hours=1),
            temperature=25.0,
            humidity=65.0,
            co2=450.0,
            light=600.0,
            source=None,
        ))

        # 5. Row too old (beyond DIURNAL_PROFILE_HISTORY_DAYS)
        readings.append(SensorReading(
            tenant_id="t1",
            greenhouse_id="g1",
            timestamp=anchor - timedelta(days=DIURNAL_PROFILE_HISTORY_DAYS + 1),
            temperature=20.0,
            humidity=55.0,
            co2=380.0,
            light=400.0,
            source=None,
        ))

        # 6. Different tenant (should not appear)
        readings.append(SensorReading(
            tenant_id="t2",
            greenhouse_id="g1",
            timestamp=anchor - timedelta(hours=1),
            temperature=22.0,
            humidity=60.0,
            co2=400.0,
            light=500.0,
            source=None,
        ))

        session.add_all(readings)
        await session.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_seed_data")
class TestQueryPhysicalHistory:
    """Test _query_physical_history filtering and shape."""

    ANCHOR = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_returns_shape_n_by_4(self, session_factory):
        async with session_factory() as session:
            result = await _query_physical_history(
                session,
                customer_id="t1",
                greenhouse_id="g1",
                before=self.ANCHOR,
            )
        assert result.ndim == 2
        assert result.shape[1] == 4

    @pytest.mark.asyncio
    async def test_excludes_synthetic_rows(self, session_factory):
        async with session_factory() as session:
            result = await _query_physical_history(
                session,
                customer_id="t1",
                greenhouse_id="g1",
                before=self.ANCHOR,
            )
        # The synthetic row had temperature 99.0 — it should not appear.
        if result.shape[0] > 0:
            assert not np.any(result[:, 0] == 99.0), (
                "Synthetic row (temperature=99.0) was not excluded"
            )

    @pytest.mark.asyncio
    async def test_keeps_null_source_rows(self, session_factory):
        async with session_factory() as session:
            result = await _query_physical_history(
                session,
                customer_id="t1",
                greenhouse_id="g1",
                before=self.ANCHOR,
            )
        # We seeded 10 normal rows with NULL source; all should be present.
        assert result.shape[0] == 10

    @pytest.mark.asyncio
    async def test_excludes_rows_at_or_after_before(self, session_factory):
        async with session_factory() as session:
            result = await _query_physical_history(
                session,
                customer_id="t1",
                greenhouse_id="g1",
                before=self.ANCHOR,
            )
        # Rows at anchor (temp=25.0) and after anchor should be excluded.
        # Only the 10 normal rows (22.0..22.9) should be in the result.
        if result.shape[0] > 0:
            assert not np.any(result[:, 0] == 25.0), (
                "Row at or after 'before' was not excluded"
            )

    @pytest.mark.asyncio
    async def test_excludes_rows_beyond_diurnal_window(self, session_factory):
        async with session_factory() as session:
            result = await _query_physical_history(
                session,
                customer_id="t1",
                greenhouse_id="g1",
                before=self.ANCHOR,
            )
        # The too-old row had temperature=20.0 and was the only one with that value.
        if result.shape[0] > 0:
            assert not np.any(result[:, 0] == 20.0), (
                "Row beyond DIURNAL_PROFILE_HISTORY_DAYS was not excluded"
            )

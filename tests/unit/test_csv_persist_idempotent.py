"""Async tests for idempotent CSV persistence."""

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.csv_persist import (
    normalize_persist_timestamp,
    persist_csv_file,
    persist_timestamp_key,
)
from src.db.models import Base, RawReading, SensorReading
from src.vendors.csv_adapter import CSVAdapter


@pytest_asyncio.fixture
async def persist_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_persist_csv_file_is_idempotent(tmp_path, persist_session):
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text(
        "timestamp,temperature,humidity,co2,light\n"
        "2026-06-17T00:00:00Z,20.0,60.0,400,500\n"
        "2026-06-17T00:05:00Z,20.1,60.1,401,501\n",
        encoding="utf-8",
    )
    adapter = CSVAdapter({"filepath": str(csv_path)})
    adapter.connect()

    first = await persist_csv_file(
        tmp_path=str(csv_path),
        tenant_id="tenant_1",
        greenhouse_id="gh_A",
        db=persist_session,
        adapter=adapter,
    )
    second = await persist_csv_file(
        tmp_path=str(csv_path),
        tenant_id="tenant_1",
        greenhouse_id="gh_A",
        db=persist_session,
        adapter=adapter,
        anomaly_scores_by_timestamp={"2026-06-17T00:00:00+00:00": 0.91},
    )

    sensor_count = await persist_session.scalar(
        select(func.count()).select_from(SensorReading).where(
            SensorReading.tenant_id == "tenant_1",
            SensorReading.greenhouse_id == "gh_A",
        )
    )
    raw_count = await persist_session.scalar(
        select(func.count()).select_from(RawReading).where(
            RawReading.tenant_id == "tenant_1",
            RawReading.greenhouse_id == "gh_A",
        )
    )

    assert first.sensor_rows_inserted == 2
    assert second.sensor_rows_inserted == 0
    assert second.sensor_rows_updated == 2
    assert sensor_count == 2
    assert raw_count == 2


def test_persist_timestamp_key_is_stable_for_naive_and_aware():
    from datetime import datetime, timezone

    naive = datetime(2026, 6, 17, 0, 0, 0)
    aware = datetime(2026, 6, 17, 0, 0, 0, tzinfo=timezone.utc)
    assert persist_timestamp_key(naive) == persist_timestamp_key(aware)
    assert normalize_persist_timestamp(naive).tzinfo is not None

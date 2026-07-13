"""Tests for the diurnal profile auto-build service (P2).

The diurnal detector (spec §9) needs per-hour baselines; this service
builds them from stored sensor_readings and persists them into
LearningState so the detector auto-enables per tenant.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.api.services.diurnal_profile_service import (
    build_diurnal_profiles,
    persist_diurnal_profiles,
)
from src.db.models import Base, LearningState, SensorReading

_TENANT = "diurnal_tenant"


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_readings(
    db: AsyncSession,
    *,
    days: int = 7,
    per_hour: int = 6,
    tenant_id: str = _TENANT,
) -> int:
    """Seed a diurnal temperature cycle: 22°C at night, 28°C at hour 12."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = []
    for day in range(days):
        for hour in range(24):
            for i in range(per_hour):
                ts = now - timedelta(days=day + 1, hours=hour, minutes=i * 10)
                temp = 25.0 + 3.0 * (1 if ts.hour == 12 else -1) * 0.5
                rows.append(
                    SensorReading(
                        tenant_id=tenant_id,
                        greenhouse_id="default",
                        timestamp=ts,
                        temperature=temp,
                        humidity=65.0,
                        co2=400.0,
                        light=500.0,
                        anomaly_score=None,
                    )
                )
    db.add_all(rows)
    await db.commit()
    return len(rows)


@pytest.mark.asyncio
async def test_build_returns_hourly_stats(db_session: AsyncSession) -> None:
    await _seed_readings(db_session)
    profiles = await build_diurnal_profiles(db_session, tenant_id=_TENANT)

    assert "temperature" in profiles
    hours = profiles["temperature"]
    assert len(hours) >= 20, "most hours should have enough samples"
    mean, std = next(iter(hours.values()))
    assert 20.0 < mean < 30.0
    assert std >= 1e-6


@pytest.mark.asyncio
async def test_build_returns_empty_for_thin_history(db_session: AsyncSession) -> None:
    """Below DIURNAL_PROFILE_MIN_ROWS the tenant is too new for baselines."""
    await _seed_readings(db_session, days=1, per_hour=1)  # 24 rows << 288
    profiles = await build_diurnal_profiles(db_session, tenant_id=_TENANT)
    assert profiles == {}


@pytest.mark.asyncio
async def test_persist_merges_into_learning_state(db_session: AsyncSession) -> None:
    await _seed_readings(db_session)
    profiles = await build_diurnal_profiles(db_session, tenant_id=_TENANT)
    await persist_diurnal_profiles(db_session, tenant_id=_TENANT, profiles=profiles)

    row = await db_session.get(LearningState, _TENANT)
    assert row is not None
    stored = row.state_json["diurnal_profiles"]
    # JSON-safe: string hour keys, list values.
    assert all(isinstance(h, str) for h in stored["temperature"])
    assert row.state_json["diurnal_profiles_built_at"]


@pytest.mark.asyncio
async def test_persist_preserves_existing_state_keys(db_session: AsyncSession) -> None:
    db_session.add(LearningState(customer_id=_TENANT, state_json={"other_key": 1}))
    await db_session.commit()

    await persist_diurnal_profiles(
        db_session,
        tenant_id=_TENANT,
        profiles={"temperature": {12: (25.0, 0.5)}},
    )
    row = await db_session.get(LearningState, _TENANT)
    assert row.state_json["other_key"] == 1
    assert row.state_json["diurnal_profiles"]["temperature"]["12"] == [25.0, 0.5]

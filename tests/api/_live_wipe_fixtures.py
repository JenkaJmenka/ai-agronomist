"""Shared fixtures and row queries for the live-wipe provenance acceptance tests.

``test_live_wipe_source_collision_acceptance.py`` and
``test_live_wipe_provenance_acceptance.py`` are one acceptance suite split
across two files for the 300-line guardrail, so they need the same tenant
constants, CSV builders and survivor queries.  This module owns them, so the
dependency is explicit and a rename fails loudly at import time — the same
arrangement ``_marker_fixtures.py`` uses for the synthetic-marker pair.

The ``live_wipe_database`` / ``live_wipe_client`` fixtures live here too rather
than in ``tests/api/conftest.py``: they bake in this suite's tenant and would
otherwise be broadcast to every api test.  Each test module imports them
explicitly, which is enough for pytest to discover them (the same re-export
pattern ``tests/integration/conftest.py`` uses).

Redis is not required: ``_clear_live_stream`` and ``invalidate_response_cache``
both swallow every exception and return ``False``, so a missing Redis only
produces warning logs.
"""

from __future__ import annotations

import datetime

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.config import SYNTHETIC_SOURCE_PREFIX
from src.db.models import Base, RawReading, SensorReading
from src.db.session import get_db

TENANT = "tenant_source_collision"
GREENHOUSE = "gh_source_collision"

# The reachable collision header.  A bare "live" header cannot reach
# RawReading.source at all: parse_raw_rows only treats a column as a timestamp
# candidate when its lowercased header contains "timestamp", "datetime", "date"
# or "time" (src/vendors/csv_adapter.py), so "live" is never read as a
# timestamp and the wipe's exact-match arm source == 'live' is unreachable from
# a CSV header.  Only the LIKE 'live:%' arm is reachable, and this header is
# what reaches it: it carries the "timestamp" keyword, trips neither the
# "update" nor the "created" exclusion, and is stored verbatim because
# parse_raw_rows reads chunk.columns before any header cleaning.
COLLIDING_TS_HEADER = "live:timestamp"
ROW_COUNT = 8

LIVE_SOURCE = "live"
LIVE_PREFIXED_SOURCE = "live:mock"

T0 = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.timezone.utc)
STEP = datetime.timedelta(minutes=5)


def upload_timestamps() -> list[datetime.datetime]:
    return [T0 + i * STEP for i in range(ROW_COUNT)]


def naive_upload_timestamps() -> list[datetime.datetime]:
    """``upload_timestamps`` as SQLite returns them — naive, still UTC."""
    return [ts.replace(tzinfo=None) for ts in upload_timestamps()]


def colliding_csv_bytes() -> bytes:
    """A CSV whose timestamp column is headed with the reserved live prefix."""
    lines = [f"{COLLIDING_TS_HEADER},temperature,humidity,co2,light"]
    for i, ts in enumerate(upload_timestamps()):
        lines.append(
            f"{ts.strftime('%Y-%m-%d %H:%M:%S')},"
            f"{21.0 + i * 0.1:.1f},{58.0 + i * 0.2:.1f},{410 + i},{300 + i}"
        )
    return "\n".join(lines).encode()


def plain_csv_bytes() -> bytes:
    """The same rows under an ordinary ``timestamp`` header."""
    return colliding_csv_bytes().decode().replace(COLLIDING_TS_HEADER, "timestamp", 1).encode()


@pytest_asyncio.fixture
async def live_wipe_database() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def live_wipe_client(live_wipe_database: AsyncSession) -> TestClient:
    async def override_get_db():
        yield live_wipe_database

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def upload_csv(client: TestClient, payload: bytes) -> dict:
    """Drive ``POST /api/upload`` and return its body."""
    response = client.post(
        "/api/upload",
        files={"file": ("readings.csv", payload, "text/csv")},
        data={"tenant_id": TENANT, "greenhouse_id": GREENHOUSE},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()


def run_live_wipe(client: TestClient) -> dict:
    """Drive ``DELETE /api/upload/live-data`` and return its body."""
    response = client.delete(
        "/api/upload/live-data",
        params={"tenant_id": TENANT, "greenhouse_id": GREENHOUSE},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()


async def raw_rows(db: AsyncSession) -> list[RawReading]:
    rows = await db.execute(
        select(RawReading).where(
            RawReading.tenant_id == TENANT, RawReading.greenhouse_id == GREENHOUSE
        )
    )
    return list(rows.scalars().all())


async def sensor_rows(db: AsyncSession) -> list[SensorReading]:
    rows = await db.execute(
        select(SensorReading).where(
            SensorReading.tenant_id == TENANT, SensorReading.greenhouse_id == GREENHOUSE
        )
    )
    return list(rows.scalars().all())


async def raw_sources(db: AsyncSession) -> set[str | None]:
    return {row.source for row in await raw_rows(db)}


async def sensor_sources(db: AsyncSession) -> set[str | None]:
    return {row.source for row in await sensor_rows(db)}


async def synthetic_raw_rows(db: AsyncSession) -> list[RawReading]:
    return [
        row for row in await raw_rows(db)
        if (row.source or "").startswith(SYNTHETIC_SOURCE_PREFIX)
    ]


async def synthetic_sensor_rows(db: AsyncSession) -> list[SensorReading]:
    return [
        row for row in await sensor_rows(db)
        if (row.source or "").startswith(SYNTHETIC_SOURCE_PREFIX)
    ]


def add_live_rows(
    db: AsyncSession, timestamp: datetime.datetime, raw_source: str = LIVE_SOURCE
) -> None:
    """Seed the pair of rows ``src/live/persistence.py`` writes for one reading."""
    db.add(
        SensorReading(
            tenant_id=TENANT,
            greenhouse_id=GREENHOUSE,
            timestamp=timestamp,
            temperature=23.0,
            humidity=55.0,
            co2=430.0,
            light=480.0,
            source=LIVE_SOURCE,
        )
    )
    db.add(
        RawReading(
            tenant_id=TENANT,
            greenhouse_id=GREENHOUSE,
            timestamp=timestamp,
            source=raw_source,
            data={"temperature": 23.0, "humidity": 55.0, "co2": 430.0, "light": 480.0},
        )
    )

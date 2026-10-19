"""Shared fixtures and row queries for the source-collision acceptance tests.

``test_live_wipe_source_collision_acceptance.py`` and
``test_live_wipe_provenance_acceptance.py`` are one acceptance suite split
across two files for the 300-line guardrail, so they need the same tenant
constants, CSV builders and survivor queries.  This module owns them, so the
dependency is explicit and a rename fails loudly at import time — the same
arrangement ``_marker_fixtures.py`` uses for the synthetic-marker pair.
``test_synthetic_wipe_source_collision_acceptance.py`` imports the same
constants, builders and queries for the ``synthetic:`` direction of the bug.

The ``live_wipe_client`` fixture lives here rather than in
``tests/api/conftest.py`` because ``conftest.py`` has no ``TestClient``
fixture at all, and the only other one — ``test_client`` in
``test_upload_routes.py`` — is module-local, so importing it would make this
suite depend on an unrelated test module's internals.  The session it binds
comes from conftest's ``db_session``: an earlier revision of this module
carried its own byte-identical in-memory-engine fixture, justified by a
docstring claiming it "bakes in this suite's tenant".  It did not — it
referenced neither ``TENANT`` nor ``GREENHOUSE`` — so it was a third copy of
one fixture in one directory and is gone.

Each test module imports ``live_wipe_client`` explicitly, which is enough for
pytest to discover it (the same re-export pattern
``tests/integration/conftest.py`` uses).

Redis is not required: ``_clear_live_stream`` and ``invalidate_response_cache``
both swallow every exception and return ``False``, so a missing Redis only
produces warning logs.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.main import app
from src.config import SYNTHETIC_SOURCE_PREFIX
from src.db.models import RawReading, SensorReading
from src.db.session import get_db

TENANT = "tenant_source_collision"
GREENHOUSE = "gh_source_collision"

# The reachable collision headers.  A bare "live" header cannot reach
# RawReading.source at all: parse_raw_rows only treats a column as a timestamp
# candidate when its lowercased header contains "timestamp", "datetime", "date"
# or "time" (src/vendors/csv_adapter.py), so "live" is never read as a
# timestamp and the wipe's exact-match arm source == 'live' is unreachable from
# a CSV header.  Only the LIKE 'live:%' arm is reachable, and this header is
# what reaches it: it carries the "timestamp" keyword, trips neither the
# "update" nor the "created" exclusion, and is stored verbatim because
# parse_raw_rows reads chunk.columns before any header cleaning.
COLLIDING_TS_HEADER = "live:timestamp"

# The same trick aimed at the other reserved prefix, reachable for the same
# three reasons.  Pre-fix this made an upload's raw rows deletable by
# DELETE /upload/inject-test-anomalies, whose raw clause is
# RawReading.source LIKE 'synthetic:%' (src/api/services/synthetic_anomalies.py).
SYNTHETIC_COLLIDING_TS_HEADER = f"{SYNTHETIC_SOURCE_PREFIX}timestamp"

PLAIN_TS_HEADER = "timestamp"
ROW_COUNT = 8

LIVE_SOURCE = "live"

# A "live:<...>" value with NO producer anywhere in the codebase.  The only
# writer of live provenance is src/live/persistence.py, which writes the bare
# literal "live" in both places it sets the column (:168 and :198); the one
# other "live:" string in src/ is src/live/hub.py:43's unrelated Redis stream
# key.  The CSV header path this suite is about was the only producer of
# "live:%" that has ever existed, so wipe_live_demo_data's LIKE 'live:%' arm
# was only ever matching this bug.  See src/config.CSV_UPLOAD_RAW_SOURCE and
# the RawReading.source comment in src/db/models.py, which both say so.
LIVE_PREFIXED_SOURCE = "live:mock"

T0 = datetime.datetime(2026, 7, 1, 9, 0, tzinfo=datetime.timezone.utc)
STEP = datetime.timedelta(minutes=5)


def upload_timestamps() -> list[datetime.datetime]:
    return [T0 + i * STEP for i in range(ROW_COUNT)]


def naive_upload_timestamps() -> list[datetime.datetime]:
    """``upload_timestamps`` as SQLite returns them — naive, still UTC.

    SQLITE-ONLY.  ``RawReading.timestamp`` is ``DateTime(timezone=True)``, and
    on Postgres that returns **aware** datetimes, so an equality against this
    list fails there even though the stored instants are identical.  That is
    consistent with the rest of this SQLite-only suite, but it is load-bearing:
    the comparison at
    ``test_live_wipe_source_collision_acceptance.py``'s timestamp assertion is
    what proves the colliding header really drove the timestamp column, i.e.
    what keeps the headline acceptance test non-vacuous.  Ported to Postgres it
    is therefore the first thing that breaks, and it will read as if the
    source-collision fix regressed.  Strip the tzinfo on both sides (or compare
    against ``upload_timestamps()`` with ``astimezone``) rather than relaxing
    the assertion — this project has already been bitten twice by
    SQLite-permissive / Postgres-strict divergence.
    """
    return [ts.replace(tzinfo=None) for ts in upload_timestamps()]


def csv_bytes_with_timestamp_header(header: str) -> bytes:
    """``ROW_COUNT`` readings whose timestamp column carries ``header``."""
    lines = [f"{header},temperature,humidity,co2,light"]
    for i, ts in enumerate(upload_timestamps()):
        lines.append(
            f"{ts.strftime('%Y-%m-%d %H:%M:%S')},"
            f"{21.0 + i * 0.1:.1f},{58.0 + i * 0.2:.1f},{410 + i},{300 + i}"
        )
    return "\n".join(lines).encode()


def colliding_csv_bytes() -> bytes:
    """A CSV whose timestamp column is headed with the reserved live prefix."""
    return csv_bytes_with_timestamp_header(COLLIDING_TS_HEADER)


def synthetic_colliding_csv_bytes() -> bytes:
    """A CSV whose timestamp column is headed with the reserved synthetic prefix."""
    return csv_bytes_with_timestamp_header(SYNTHETIC_COLLIDING_TS_HEADER)


def plain_csv_bytes() -> bytes:
    """The same rows under an ordinary ``timestamp`` header."""
    return csv_bytes_with_timestamp_header(PLAIN_TS_HEADER)


@pytest.fixture
def live_wipe_client(db_session: AsyncSession) -> TestClient:
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def upload_csv(client: TestClient, payload: bytes) -> dict:
    """Drive ``POST /api/upload``, **assert HTTP 200**, and return its body.

    The status assertion lives here rather than at each of the call sites, so
    a test that calls this helper and then asserts once is making two
    assertions, not one.
    """
    response = client.post(
        "/api/upload",
        files={"file": ("readings.csv", payload, "text/csv")},
        data={"tenant_id": TENANT, "greenhouse_id": GREENHOUSE},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()


def run_live_wipe(client: TestClient) -> dict:
    """Drive ``DELETE /api/upload/live-data``, **assert HTTP 200**, return body.

    Same hidden-assertion caveat as ``upload_csv``.
    """
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


def add_one_live_sensor_row_and_one_live_raw_row(
    db: AsyncSession, timestamp: datetime.datetime, raw_source: str = LIVE_SOURCE
) -> None:
    """Seed the TWO rows ``src/live/persistence.py`` writes for one reading.

    Deliberately spelled out in the name, because the counts the callers
    assert on are not symmetric with the number of calls:

    - exactly one ``SensorReading``, whose ``source`` is **always**
      ``LIVE_SOURCE``.  ``raw_source`` does not touch it;
    - exactly one ``RawReading``, whose ``source`` is ``raw_source``.

    So two calls produce ``deleted_sensor == 2`` through the wipe's single
    exact-match sensor clause, while ``deleted_raw == 2`` may come from two
    different raw arms depending on the ``raw_source`` values passed.
    """
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

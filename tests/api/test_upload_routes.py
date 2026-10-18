"""Tests for the legacy /api/upload routes: CSV ingest, clearing, test-anomaly injection."""

import datetime
import io

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.models import Base, RawReading, SensorReading
from src.db.session import get_db

TENANT = "tenant_upload_routes"
NOW = datetime.datetime.now(datetime.timezone.utc)


def _csv_bytes(rows: int = 25) -> bytes:
    lines = ["timestamp,temperature,humidity,co2,light"]
    for i in range(rows):
        ts = (NOW - datetime.timedelta(minutes=5 * (rows - i))).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts},{20 + i * 0.1:.1f},{60 + i * 0.05:.2f},{400 + i},{100 + i}")
    return "\n".join(lines).encode()


@pytest_asyncio.fixture
async def test_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def test_client(test_database):
    async def override_get_db():
        yield test_database

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


async def _count(db, model, tenant=TENANT):
    result = await db.execute(
        select(func.count(model.id)).where(model.tenant_id == tenant)
    )
    return result.scalar()


class TestUploadCsv:
    async def test_valid_csv_persists_both_tables(self, test_client, test_database):
        response = test_client.post(
            "/api/upload",
            files={"file": ("data.csv", io.BytesIO(_csv_bytes()), "text/csv")},
            data={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["uploaded"] == 25
        assert payload["ranges"]["temperature"]["min"] == pytest.approx(20.0)
        assert await _count(test_database, SensorReading) == 25
        assert await _count(test_database, RawReading) == 25

    def test_non_csv_extension_rejected(self, test_client):
        response = test_client.post(
            "/api/upload",
            files={"file": ("data.txt", io.BytesIO(b"x"), "text/plain")},
            data={"tenant_id": TENANT},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_unparseable_csv_rejected(self, test_client):
        response = test_client.post(
            "/api/upload",
            files={"file": ("data.csv", io.BytesIO(b"\x00\x01\x02"), "text/csv")},
            data={"tenant_id": TENANT},
        )
        assert response.status_code in (
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    def test_header_only_csv_rejected(self, test_client):
        content = b"timestamp,temperature,humidity,co2,light\n"
        response = test_client.post(
            "/api/upload",
            files={"file": ("data.csv", io.BytesIO(content), "text/csv")},
            data={"tenant_id": TENANT},
        )
        assert response.status_code in (
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class TestClearTenantData:
    async def test_clears_only_target_tenant(self, test_client, test_database):
        test_client.post(
            "/api/upload",
            files={"file": ("data.csv", io.BytesIO(_csv_bytes()), "text/csv")},
            data={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        response = test_client.delete(
            "/api/upload/data",
            params={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["cleared"] is True
        assert await _count(test_database, SensorReading) == 0
        assert await _count(test_database, RawReading) == 0

    async def test_clear_also_removes_synthetic_inject_rows(
        self, test_client, test_database
    ):
        """Clear all must wipe inject-test rows, not only CSV uploads."""
        test_client.post(
            "/api/upload/inject-test-anomalies",
            data={
                "tenant_id": TENANT,
                "greenhouse_id": "default",
                "n_normal": 10,
                "n_anomalies": 3,
                "anomaly_type": "temperature_spike",
            },
        )
        assert await _count(test_database, SensorReading) == 13

        response = test_client.delete(
            "/api/upload/data",
            params={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["cleared"] is True
        assert body["deleted_sensor"] == 13
        assert await _count(test_database, SensorReading) == 0
        assert await _count(test_database, RawReading) == 0


class TestInjectTestAnomalies:
    async def test_inject_without_raw_sample_uses_canonical_stats(
        self, test_client, test_database
    ):
        response = test_client.post(
            "/api/upload/inject-test-anomalies",
            data={
                "tenant_id": TENANT,
                "greenhouse_id": "default",
                "n_normal": 20,
                "n_anomalies": 4,
                "anomaly_type": "temperature_spike",
            },
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["injected"] == 24
        assert payload["raw_injected"] == 0  # no raw sample → canonical only
        assert set(payload["columns_used"]) == {"temperature", "humidity", "co2", "light"}
        assert await _count(test_database, SensorReading) == 24

    async def test_inject_with_raw_sample_targets_matching_columns(
        self, test_client, test_database
    ):
        for i in range(10):
            test_database.add(
                RawReading(
                    tenant_id=TENANT,
                    greenhouse_id="default",
                    timestamp=NOW - datetime.timedelta(minutes=i),
                    source="csv",
                    data={"measured_greenhouse_temp": 21.0 + i * 0.1, "wind_speed": 3.0},
                )
            )
        await test_database.commit()

        response = test_client.post(
            "/api/upload/inject-test-anomalies",
            data={
                "tenant_id": TENANT,
                "greenhouse_id": "default",
                "n_normal": 10,
                "n_anomalies": 2,
                "anomaly_type": "gradual_drift",
                "window_start": "2026-06-01T00:00:00",
                "window_end": "2026-06-02T00:00:00",
            },
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["injected"] == 12
        assert payload["raw_injected"] == 12
        assert "measured_greenhouse_temp" in payload["columns_used"]
        assert payload["window_hours"] == 24

    def test_unknown_type_rejected(self, test_client):
        response = test_client.post(
            "/api/upload/inject-test-anomalies",
            data={"tenant_id": TENANT, "anomaly_type": "volcano"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_invalid_window_rejected(self, test_client):
        response = test_client.post(
            "/api/upload/inject-test-anomalies",
            data={
                "tenant_id": TENANT,
                "anomaly_type": "random",
                "window_start": "not-a-date",
                "window_end": "2026-06-02T00:00:00",
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_inverted_window_rejected(self, test_client):
        response = test_client.post(
            "/api/upload/inject-test-anomalies",
            data={
                "tenant_id": TENANT,
                "anomaly_type": "random",
                "window_start": "2026-06-02T00:00:00",
                "window_end": "2026-06-01T00:00:00",
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestRemoveSyntheticAnomalies:
    async def test_removes_synthetic_keeps_real(self, test_client, test_database):
        test_client.post(
            "/api/upload",
            files={"file": ("data.csv", io.BytesIO(_csv_bytes(10)), "text/csv")},
            data={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        test_client.post(
            "/api/upload/inject-test-anomalies",
            data={
                "tenant_id": TENANT,
                "greenhouse_id": "default",
                "n_normal": 5,
                "n_anomalies": 2,
                "anomaly_type": "co2_surge",
            },
        )
        assert await _count(test_database, SensorReading) == 17

        response = test_client.delete(
            "/api/upload/inject-test-anomalies",
            params={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        assert response.status_code == status.HTTP_200_OK
        # Real CSV rows survive; all synthetic rows gone.
        assert await _count(test_database, SensorReading) == 10
        assert await _count(test_database, RawReading) == 10

    async def test_remove_inject_keeps_live_tagged_rows(self, test_client, test_database):
        """Inject cleanup must not own live demo rows (source='live', anomaly_type=NULL)."""
        live_ts = NOW - datetime.timedelta(minutes=1)
        test_database.add(
            SensorReading(
                tenant_id=TENANT,
                greenhouse_id="default",
                timestamp=live_ts,
                temperature=22.0,
                humidity=60.0,
                co2=400.0,
                light=500.0,
                source="live",  # post-migration provenance tag on SensorReading
            )
        )
        test_database.add(
            RawReading(
                tenant_id=TENANT,
                greenhouse_id="default",
                timestamp=live_ts,
                source="live",
                data={"temperature": 22.0, "humidity": 60.0, "co2": 400.0, "light": 500.0},
            )
        )
        await test_database.commit()
        test_client.post(
            "/api/upload/inject-test-anomalies",
            data={
                "tenant_id": TENANT,
                "greenhouse_id": "default",
                "n_normal": 4,
                "n_anomalies": 1,
                "anomaly_type": "temperature_spike",
            },
        )
        assert await _count(test_database, SensorReading) == 6

        response = test_client.delete(
            "/api/upload/inject-test-anomalies",
            params={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        assert response.status_code == status.HTTP_200_OK
        assert await _count(test_database, SensorReading) == 1
        assert await _count(test_database, RawReading) == 1
        remaining = (
            await test_database.execute(select(SensorReading).where(SensorReading.tenant_id == TENANT))
        ).scalar_one()
        assert remaining.source == "live"

    def test_unknown_type_rejected(self, test_client):
        response = test_client.delete(
            "/api/upload/inject-test-anomalies",
            params={"tenant_id": TENANT, "anomaly_type": "volcano"},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestRemoveLiveDemoData:
    async def test_removes_live_keeps_csv_and_inject(self, test_client, test_database):
        """Only a ``source='live'`` SensorReading is wiped; CSV and inject stay.

        Seeded SensorReadings, by the ``source`` the wipe keys off:
          8  CSV upload rows   — source NULL (an upload sends no source)
          4  inject rows       — "synthetic:context" x3, "synthetic:humidity_drop"
          1  live row          — source "live"
          1  untagged row      — source NULL, at a timestamp a live RawReading holds
          -- 14 total

        ``wipe_live_demo_data`` matches ``source == 'live'`` and nothing else,
        so exactly 1 of those 14 is deleted.  This assertion read ``== 2``
        before: the wipe also had a fallback matching ``source IS NULL`` rows
        sharing a timestamp with a live RawReading, which caught the untagged
        row — and would equally have caught any of the 8 CSV rows whose
        timestamp coincided with a live one.  The RawReading side is unchanged
        at 2: both seeded live raws match, while the CSV raws now carry the
        fixed "csv" marker.
        """
        test_client.post(
            "/api/upload",
            files={"file": ("data.csv", io.BytesIO(_csv_bytes(8)), "text/csv")},
            data={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        test_client.post(
            "/api/upload/inject-test-anomalies",
            data={
                "tenant_id": TENANT,
                "greenhouse_id": "default",
                "n_normal": 3,
                "n_anomalies": 1,
                "anomaly_type": "humidity_drop",
            },
        )
        live_ts = NOW - datetime.timedelta(minutes=2)
        legacy_ts = NOW - datetime.timedelta(minutes=3)
        test_database.add(
            SensorReading(
                tenant_id=TENANT,
                greenhouse_id="default",
                timestamp=live_ts,
                temperature=23.0,
                humidity=55.0,
                co2=410.0,
                light=480.0,
                source="live",  # post-migration provenance tag (was anomaly_type='live')
            )
        )
        test_database.add(
            RawReading(
                tenant_id=TENANT,
                greenhouse_id="default",
                timestamp=live_ts,
                source="live",
                data={"temperature": 23.0, "humidity": 55.0, "co2": 410.0, "light": 480.0},
            )
        )
        # Untagged SensorReading (source=None, anomaly_type=None) sharing a
        # timestamp with a live RawReading. The deleted timestamp fallback used
        # to wipe this row; it must now survive, because source IS NULL is the
        # state a plain CSV upload's rows are in too (ADR-010).
        test_database.add(
            SensorReading(
                tenant_id=TENANT,
                greenhouse_id="default",
                timestamp=legacy_ts,
                temperature=24.0,
                humidity=56.0,
                co2=420.0,
                light=490.0,
                anomaly_type=None,
            )
        )
        test_database.add(
            RawReading(
                tenant_id=TENANT,
                greenhouse_id="default",
                timestamp=legacy_ts,
                source="live",
                data={"temperature": 24.0, "humidity": 56.0, "co2": 420.0, "light": 490.0},
            )
        )
        await test_database.commit()

        assert await _count(test_database, SensorReading) == 14  # 8 csv + 4 inject + 2 live
        assert await _count(test_database, RawReading) == 14  # 8 csv + 4 inject + 2 live

        response = test_client.delete(
            "/api/upload/live-data",
            params={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["cleared_live"] is True
        assert body["deleted_sensor"] == 1  # the one source='live' row, nothing else
        assert body["deleted_raw"] == 2  # both live raws; CSV raws carry "csv"
        assert body["live_persistence_suppressed"] is True
        assert await _count(test_database, SensorReading) == 13  # 14 - 1
        assert await _count(test_database, RawReading) == 12  # 14 - 2
        live_left = await test_database.execute(
            select(func.count(SensorReading.id)).where(
                SensorReading.tenant_id == TENANT,
                SensorReading.source == "live",
            )
        )
        assert live_left.scalar() == 0
        untagged_left = await test_database.execute(
            select(func.count(SensorReading.id)).where(
                SensorReading.tenant_id == TENANT,
                SensorReading.timestamp == legacy_ts,
            )
        )
        assert untagged_left.scalar() == 1, "the untagged row must survive the wipe"

    async def test_removes_only_target_greenhouse(self, test_client, test_database):
        other_gh = "gh-other"
        for gh, source in (("default", "live"), (other_gh, "live")):
            test_database.add(
                SensorReading(
                    tenant_id=TENANT,
                    greenhouse_id=gh,
                    timestamp=NOW,
                    temperature=22.0,
                    humidity=60.0,
                    co2=400.0,
                    light=500.0,
                    source="live",  # post-migration provenance tag
                )
            )
            test_database.add(
                RawReading(
                    tenant_id=TENANT,
                    greenhouse_id=gh,
                    timestamp=NOW,
                    source=source,
                    data={"temperature": 22.0},
                )
            )
        await test_database.commit()

        response = test_client.delete(
            "/api/upload/live-data",
            params={"tenant_id": TENANT, "greenhouse_id": "default"},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["deleted_raw"] == 1
        remaining_other = await test_database.execute(
            select(func.count(RawReading.id)).where(
                RawReading.tenant_id == TENANT,
                RawReading.greenhouse_id == other_gh,
            )
        )
        assert remaining_other.scalar() == 1

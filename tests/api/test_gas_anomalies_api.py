"""Tests for /api/gas endpoints (summary + readings, synthetic exclusion).

Climate ``co2`` must not be treated as gas ``gas_co2``.
"""

import datetime

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.models import Base, RawReading
from src.db.session import get_db

TENANT = "tenant_gas"
NOW = datetime.datetime.now(datetime.timezone.utc)


def _row(minutes_ago: int, data: dict, source: str | None = "csv") -> RawReading:
    return RawReading(
        tenant_id=TENANT,
        greenhouse_id="default",
        timestamp=NOW - datetime.timedelta(minutes=minutes_ago),
        source=source,
        data=data,
    )


@pytest_asyncio.fixture
async def test_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        # 30 baseline rows with dedicated gas_co2 + c2h4; one outlier; synthetic;
        # climate-only co2 row must not create a gas_co2 series.
        for i in range(30):
            session.add(_row(120 - i, {"gas_co2": 400.0 + (i % 5), "c2h4": 1.0}))
        session.add(_row(10, {"gas_co2": 5000.0, "c2h4": 1.0}))
        session.add(_row(5, {"gas_co2": 9999.0}, source="synthetic:test"))
        session.add(_row(4, {"gas_co2": "not-a-number"}))
        session.add(_row(3, {"co2": 420.0, "temperature": 22.0}))
        await session.commit()
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


class TestGasSummary:
    def test_baseline_and_outlier_flagged(self, test_client):
        response = test_client.get(
            "/api/gas/summary",
            params={"tenant_id": TENANT, "greenhouse_id": "default", "hours": 24},
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["window_hours"] == 24
        cols = {c["column"]: c for c in payload["gas_columns"]}
        assert "gas_co2" in cols
        gas_co2 = cols["gas_co2"]
        anomalous = [r for r in gas_co2["readings"] if r["is_anomaly"]]
        assert len(anomalous) >= 1
        assert any(r["value"] == 5000.0 for r in anomalous)
        # synthetic row (9999.0) must not appear anywhere
        assert all(r["value"] != 9999.0 for r in gas_co2["readings"])
        assert gas_co2["anomaly_count"] == len(anomalous)

    def test_climate_co2_is_not_aliased_to_gas_co2(self, test_client):
        """Environmental co2 alone must not produce a gas_co2 column entry from aliasing."""
        response = test_client.get(
            "/api/gas/summary",
            params={"tenant_id": TENANT, "greenhouse_id": "default", "hours": 24},
        )
        cols = {c["column"]: c for c in response.json()["gas_columns"]}
        gas_co2 = cols["gas_co2"]
        # Climate-only row has co2=420; must not appear as a gas_co2 reading.
        assert all(r["value"] != 420.0 for r in gas_co2["readings"])

    def test_current_value_is_latest_numeric(self, test_client):
        response = test_client.get(
            "/api/gas/summary",
            params={"tenant_id": TENANT, "greenhouse_id": "default", "hours": 24},
        )
        cols = {c["column"]: c for c in response.json()["gas_columns"]}
        # newest real numeric reading is the 5000.0 outlier (10 min ago);
        # the "not-a-number" row is newer but non-numeric.
        assert cols["gas_co2"]["current_value"] == 5000.0

    def test_empty_window_returns_no_columns(self, test_client):
        response = test_client.get(
            "/api/gas/summary",
            params={"tenant_id": "nobody", "greenhouse_id": "default", "hours": 24},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["gas_columns"] == []


class TestGasReadings:
    def test_readings_exclude_synthetic(self, test_client):
        response = test_client.get(
            "/api/gas/readings",
            params={"tenant_id": TENANT, "greenhouse_id": "default", "hours": 24},
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        # 30 baseline + outlier + non-numeric + climate-only co2 row
        assert payload["total"] == 33
        values = [r["data"].get("gas_co2") for r in payload["readings"]]
        assert 9999.0 not in values
        assert 420.0 not in values  # climate co2 must not appear under gas_co2

    def test_validation_rejects_bad_hours(self, test_client):
        response = test_client.get(
            "/api/gas/readings",
            params={"tenant_id": TENANT, "greenhouse_id": "default", "hours": 0},
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

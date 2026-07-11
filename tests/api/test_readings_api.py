"""Tests for the /api/readings endpoints (dashboard read path, item C3)."""

import datetime

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.models import Base, Incident, RawReading, SensorReading
from src.db.session import get_db

TENANT = "tenant_readings"
BASE_TS = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
ROWS = 48


@pytest_asyncio.fixture
async def test_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        for i in range(ROWS):
            ts = BASE_TS + datetime.timedelta(hours=i)
            session.add(
                SensorReading(
                    tenant_id=TENANT,
                    greenhouse_id="default",
                    timestamp=ts,
                    temperature=20.0 + i * 0.1,
                    humidity=50.0,
                    co2=400.0,
                    light=1000.0,
                    anomaly_score=0.9 if i == 10 else None,
                )
            )
            session.add(
                RawReading(
                    tenant_id=TENANT,
                    greenhouse_id="default",
                    timestamp=ts,
                    source="csv",
                    data={"temperature": 20.0 + i * 0.1, "outdoor_temp": 15.0},
                )
            )
        session.add(
            Incident(
                tenant_id=TENANT,
                greenhouse_id="default",
                detected_at=BASE_TS + datetime.timedelta(hours=5),
                anomaly_score=0.95,
                fault_type="spike_fault",
                sequence=[[20.0, 50.0, 400.0, 1000.0]] * 20,
                sequence_length=20,
                embedding=[0.0] * 8,
                embedding_uncertainty=0.1,
            )
        )
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


class TestWindowEndpoint:
    def test_window_filters_and_merges_scores(self, test_client):
        start = (BASE_TS + datetime.timedelta(hours=8)).isoformat()
        end = (BASE_TS + datetime.timedelta(hours=12)).isoformat()
        response = test_client.get(
            "/api/readings/window",
            params={"tenant_id": TENANT, "start": start, "end": end},
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["count"] == 5
        # Raw fidelity preserved (extra CSV column) + canonical score merged in.
        scored = [r for r in payload["rows"] if r.get("anomaly_score") is not None]
        assert len(scored) == 1
        assert scored[0]["anomaly_score"] == pytest.approx(0.9)
        assert all("outdoor_temp" in r for r in payload["rows"])

    def test_window_respects_limit_cap(self, test_client):
        start = BASE_TS.isoformat()
        end = (BASE_TS + datetime.timedelta(hours=ROWS)).isoformat()
        response = test_client.get(
            "/api/readings/window",
            params={"tenant_id": TENANT, "start": start, "end": end, "limit": 7},
        )
        assert response.json()["count"] == 7

    def test_window_rejects_bad_timestamp(self, test_client):
        response = test_client.get(
            "/api/readings/window",
            params={"tenant_id": TENANT, "start": "not-a-date", "end": "also-bad"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestRecentEndpoint:
    def test_recent_is_anchored_to_newest_data(self, test_client):
        # Data is historical; a wall-clock window would be empty. The endpoint
        # anchors to the newest stored timestamp instead.
        response = test_client.get(
            "/api/readings/recent",
            params={"tenant_id": TENANT, "hours": 24},
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["count"] == 25  # inclusive 24h window at 1-row/hour
        assert payload["max_timestamp"] is not None

    def test_recent_empty_tenant(self, test_client):
        response = test_client.get(
            "/api/readings/recent", params={"tenant_id": "ghost"}
        )
        payload = response.json()
        assert payload["rows"] == []
        assert payload["max_timestamp"] is None


def test_bounds_endpoint(test_client):
    response = test_client.get(
        "/api/readings/bounds", params={"tenant_id": TENANT}
    )
    payload = response.json()
    assert payload["min_timestamp"].startswith("2026-06-01T12:00")
    assert payload["max_timestamp"].startswith("2026-06-03T11:00")


def test_greenhouses_endpoint(test_client):
    response = test_client.get(
        "/api/readings/greenhouses", params={"tenant_id": TENANT}
    )
    assert response.json()["greenhouses"] == ["default"]


def test_raw_exists_endpoint(test_client):
    assert (
        test_client.get(
            "/api/readings/raw/exists", params={"tenant_id": TENANT}
        ).json()["exists"]
        is True
    )
    assert (
        test_client.get(
            "/api/readings/raw/exists", params={"tenant_id": "ghost"}
        ).json()["exists"]
        is False
    )


def test_incidents_endpoint(test_client):
    response = test_client.get(
        "/api/readings/incidents", params={"tenant_id": TENANT}
    )
    payload = response.json()
    assert payload["count"] == 1
    assert payload["rows"][0]["fault_type"] == "spike_fault"


class TestDownsamplingParams:
    """G1: max_points/include_scores wiring on /window (route-level; the LTTB
    algorithm itself is covered by tests/services/test_downsample.py)."""

    def test_max_points_unset_is_byte_identical_to_pre_g1(self, test_client):
        response = test_client.get(
            "/api/readings/window",
            params={"tenant_id": TENANT, "start": BASE_TS.isoformat(), "end": (
                BASE_TS + datetime.timedelta(hours=ROWS)
            ).isoformat()},
        )
        payload = response.json()
        assert set(payload.keys()) == {"rows", "count"}
        assert "downsampled" not in payload
        assert "original_count" not in payload

    def test_max_points_below_floor_rejected(self, test_client):
        response = test_client.get(
            "/api/readings/window",
            params={
                "tenant_id": TENANT,
                "start": BASE_TS.isoformat(),
                "end": (BASE_TS + datetime.timedelta(hours=ROWS)).isoformat(),
                "max_points": 50,
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_max_points_above_ceiling_rejected(self, test_client):
        response = test_client.get(
            "/api/readings/window",
            params={
                "tenant_id": TENANT,
                "start": BASE_TS.isoformat(),
                "end": (BASE_TS + datetime.timedelta(hours=ROWS)).isoformat(),
                "max_points": 20000,
            },
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_max_points_over_row_count_is_a_noop(self, test_client):
        # ROWS (48) is under the floor (100), so this row set can never
        # actually exceed max_points; confirms the no-op path stays additive.
        response = test_client.get(
            "/api/readings/window",
            params={
                "tenant_id": TENANT,
                "start": BASE_TS.isoformat(),
                "end": (BASE_TS + datetime.timedelta(hours=ROWS)).isoformat(),
                "max_points": 100,
            },
        )
        payload = response.json()
        assert payload["count"] == ROWS
        assert "downsampled" not in payload

    def test_include_scores_false_strips_anomaly_score(self, test_client):
        response = test_client.get(
            "/api/readings/window",
            params={
                "tenant_id": TENANT,
                "start": BASE_TS.isoformat(),
                "end": (BASE_TS + datetime.timedelta(hours=ROWS)).isoformat(),
                "include_scores": False,
            },
        )
        payload = response.json()
        assert payload["rows"]
        assert all("anomaly_score" not in r for r in payload["rows"])

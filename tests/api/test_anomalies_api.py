"""Route tests for /api/anomalies/groups (G3) and /enriched (G4)."""

import datetime

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.models import Base, Incident, RawReading, SensorReading
from src.db.session import get_db

TENANT = "tenant_anomalies"
BASE_TS = datetime.datetime(2026, 6, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
ROWS = 48
# Hour → anomaly score for the flagged rows (spread over two calendar days).
SCORED_HOURS = {5: 0.9, 6: 0.7, 30: 0.85}
INCIDENT_HOUR = 5


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
                    temperature=22.0,
                    humidity=60.0,
                    co2=400.0,
                    light=500.0,
                    anomaly_score=SCORED_HOURS.get(i),
                )
            )
            session.add(
                RawReading(
                    tenant_id=TENANT,
                    greenhouse_id="default",
                    timestamp=ts,
                    source="csv",
                    data={"temperature": 22.0, "humidity": 60.0},
                )
            )
        session.add(
            Incident(
                tenant_id=TENANT,
                greenhouse_id="default",
                detected_at=BASE_TS + datetime.timedelta(hours=INCIDENT_HOUR),
                anomaly_score=0.95,
                fault_type="spike_fault",
                root_cause="Fan failure",
                root_cause_details={"affected_sensors": ["temperature"]},
                sequence=[[22.0, 60.0, 400.0, 500.0]] * 20,
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


WINDOW = {
    "tenant_id": TENANT,
    "start": BASE_TS.isoformat(),
    "end": (BASE_TS + datetime.timedelta(hours=ROWS)).isoformat(),
    "threshold": 0.5,
}


class TestEnrichedEndpoint:
    def test_rows_ranked_by_score_with_incident_enrichment(self, test_client):
        response = test_client.get("/api/anomalies/enriched", params=WINDOW)
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["anomaly_count"] == len(SCORED_HOURS)
        assert payload["count"] == len(SCORED_HOURS)
        scores = [row["anomaly_score"] for row in payload["rows"]]
        assert scores == sorted(scores, reverse=True)
        top = payload["rows"][0]
        # Hour 5 (score 0.9) matches the incident within the ±15 min join.
        assert top["fault_type"] == "spike_fault"
        assert top["root_cause"] == "Fan failure"
        assert isinstance(top["affected_sensors"], list)
        assert top["reasoning"]

    def test_limit_caps_rows_but_not_anomaly_count(self, test_client):
        response = test_client.get(
            "/api/anomalies/enriched", params={**WINDOW, "limit": 1}
        )
        payload = response.json()
        assert payload["count"] == 1
        assert payload["anomaly_count"] == len(SCORED_HOURS)
        assert payload["rows"][0]["anomaly_score"] == pytest.approx(0.9)

    def test_rows_without_incident_still_enriched(self, test_client):
        response = test_client.get("/api/anomalies/enriched", params=WINDOW)
        rows = response.json()["rows"]
        unmatched = [r for r in rows if r["incident_id"] is None]
        assert unmatched
        assert all(r["fault_type"] for r in unmatched)
        assert all(r["root_cause"] is None for r in unmatched)

    def test_empty_window_yields_empty(self, test_client):
        response = test_client.get(
            "/api/anomalies/enriched",
            params={**WINDOW, "tenant_id": "ghost"},
        )
        payload = response.json()
        assert payload == {"rows": [], "count": 0, "anomaly_count": 0}


class TestGroupsEndpoint:
    def test_group_by_date_spans_calendar_days(self, test_client):
        response = test_client.get(
            "/api/anomalies/groups", params={**WINDOW, "group_by": "date"}
        )
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["group_by"] == "date"
        assert payload["anomaly_count"] == len(SCORED_HOURS)
        by_key = {g["key"]: g for g in payload["groups"]}
        assert by_key["2026-06-01"]["count"] == 2  # hours 5 and 6
        assert by_key["2026-06-02"]["count"] == 1  # hour 30
        assert by_key["2026-06-01"]["top_score"] == pytest.approx(0.9)
        assert len(by_key["2026-06-01"]["sample_ids"]) == 2

    def test_group_by_type_uses_enriched_fault_types(self, test_client):
        response = test_client.get(
            "/api/anomalies/groups", params={**WINDOW, "group_by": "type"}
        )
        groups = response.json()["groups"]
        assert groups
        assert sum(g["count"] for g in groups) == len(SCORED_HOURS)
        assert "spike_fault" in {g["key"] for g in groups}

    def test_group_by_sensor_returns_sensor_keys(self, test_client):
        response = test_client.get(
            "/api/anomalies/groups", params={**WINDOW, "group_by": "sensor"}
        )
        groups = response.json()["groups"]
        assert groups
        assert all(g["count"] >= 1 and g["sample_ids"] for g in groups)

    def test_invalid_group_by_rejected(self, test_client):
        response = test_client.get(
            "/api/anomalies/groups", params={**WINDOW, "group_by": "severity"}
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_rejects_bad_timestamp(self, test_client):
        response = test_client.get(
            "/api/anomalies/groups",
            params={
                "tenant_id": TENANT,
                "start": "nope",
                "end": "nope",
                "threshold": 0.5,
                "group_by": "date",
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

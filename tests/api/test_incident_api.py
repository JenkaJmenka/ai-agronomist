"""API integration tests for incident memory endpoints (Gap 2)."""

from __future__ import annotations

import datetime
from typing import List

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.config import LATENT_DIM, SEQ_LEN
from src.db.incident_memory import IncidentMemory
from src.db.models import Base
from src.db.session import get_db


def unit_embedding(axis: int = 0, sign: float = 1.0) -> List[float]:
    embedding = [0.0] * LATENT_DIM
    embedding[axis] = sign
    return embedding


def sample_sequence() -> List[List[float]]:
    row = [22.0, 60.0, 400.0, 500.0]
    return [row[:] for _ in range(SEQ_LEN)]


def store_payload(tenant_id: str = "tenant_a", **overrides) -> dict:
    payload = {
        "tenant_id": tenant_id,
        "greenhouse_id": "gh_1",
        "embedding": unit_embedding(0),
        "anomaly_score": 0.75,
        "sequence": sample_sequence(),
        "embedding_uncertainty": 0.15,
        "root_cause": "boiler_failure",
        "tags": ["heating"],
        "notes": "Test incident",
    }
    payload.update(overrides)
    return payload


@pytest_asyncio.fixture
async def incident_db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def incident_client(incident_db_session: AsyncSession) -> TestClient:
    async def override_get_db():
        yield incident_db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_store_incident_returns_201(incident_client: TestClient) -> None:
    response = incident_client.post("/api/incidents/store", json=store_payload())
    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["status"] == "stored"
    assert "incident_id" in body


def test_similar_rejects_wrong_embedding_dimension(incident_client: TestClient) -> None:
    bad_embedding = [0.1] * (LATENT_DIM - 1)
    response = incident_client.get(
        "/api/incidents/similar",
        params={"embedding": bad_embedding, "tenant_id": "tenant_a"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


@pytest.mark.asyncio
async def test_similar_returns_distance_ordering(
    incident_client: TestClient, incident_db_session: AsyncSession
) -> None:
    memory = IncidentMemory(incident_db_session)
    query = unit_embedding(0)
    for embedding in [unit_embedding(0, -1.0), unit_embedding(0), unit_embedding(1)]:
        await memory.store_incident(
            tenant_id="tenant_a",
            embedding=embedding,
            anomaly_score=0.8,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
        )

    response = incident_client.get(
        "/api/incidents/similar",
        params={"embedding": query, "tenant_id": "tenant_a", "k": 3},
    )
    assert response.status_code == status.HTTP_200_OK
    incidents = response.json()
    assert len(incidents) == 3
    distances = [entry["distance"] for entry in incidents]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_suggest_fixes_endpoint(
    incident_client: TestClient, incident_db_session: AsyncSession
) -> None:
    memory = IncidentMemory(incident_db_session)
    embedding = unit_embedding(0)
    incident_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=embedding,
        anomaly_score=0.9,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.resolve_incident(
        incident_id=incident_id,
        tenant_id="tenant_a",
        root_cause="boiler_failure",
        actions_taken=["restart_heating"],
        actions_effective=True,
    )

    response = incident_client.get(
        "/api/incidents/suggest-fixes",
        params={"embedding": embedding, "tenant_id": "tenant_a"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["confidence"] >= 1
    assert body["most_likely_cause"]["root_cause"] == "boiler_failure"
    assert any(action["action"] == "restart_heating" for action in body["recommended_actions"])


def test_resolve_unknown_incident_returns_404_message(incident_client: TestClient) -> None:
    response = incident_client.post(
        "/api/incidents/resolve/nonexistent-id",
        json={
            "tenant_id": "tenant_a",
            "root_cause": "unknown",
            "actions_taken": ["inspect"],
            "actions_effective": False,
        },
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert "message" in response.json()
    assert "detail" not in response.json()


@pytest.mark.asyncio
async def test_resolve_cross_tenant_returns_404(
    incident_client: TestClient, incident_db_session: AsyncSession
) -> None:
    memory = IncidentMemory(incident_db_session)
    incident_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=unit_embedding(0),
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )

    response = incident_client.post(
        f"/api/incidents/resolve/{incident_id}",
        json={
            "tenant_id": "tenant_b",
            "root_cause": "intrusion",
            "actions_taken": ["lockdown"],
            "actions_effective": False,
        },
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert "message" in response.json()


@pytest.mark.asyncio
async def test_statistics_endpoint(
    incident_client: TestClient, incident_db_session: AsyncSession
) -> None:
    memory = IncidentMemory(incident_db_session)
    incident_id = await memory.store_incident(
        tenant_id="tenant_a",
        embedding=unit_embedding(0),
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.resolve_incident(
        incident_id=incident_id,
        tenant_id="tenant_a",
        root_cause="humidity_spike",
        actions_taken=["vent"],
        actions_effective=True,
    )

    response = incident_client.get(
        "/api/incidents/statistics", params={"tenant_id": "tenant_a"}
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total_incidents"] == 1
    assert body["resolved_incidents"] == 1
    assert body["resolved_rate"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_recurring_issues_endpoint(
    incident_client: TestClient, incident_db_session: AsyncSession
) -> None:
    memory = IncidentMemory(incident_db_session)
    embedding = unit_embedding(0)
    for _ in range(3):
        incident_id = await memory.store_incident(
            tenant_id="tenant_a",
            embedding=embedding,
            anomaly_score=0.5,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
        )
        await memory.resolve_incident(
            incident_id=incident_id,
            tenant_id="tenant_a",
            root_cause="pump_failure",
            actions_taken=["restart_pump"],
            actions_effective=True,
        )

    response = incident_client.get(
        "/api/incidents/recurring-issues",
        params={"tenant_id": "tenant_a", "min_occurrences": 3},
    )
    assert response.status_code == status.HTTP_200_OK
    issues = response.json()
    assert len(issues) == 1
    assert issues[0]["root_cause"] == "pump_failure"
    assert issues[0]["occurrences"] == 3

"""Tests for API key authentication and learning-state persistence."""

from __future__ import annotations

from typing import Generator

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.auth import clear_test_api_keys, register_test_api_key
from src.api.main import app
from src.api.services.orchestrator_registry import (
    clear_orchestrator_cache,
    get_orchestrator,
)
from src.db.models import Base
from src.db.session import get_db


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def client(db_session: AsyncSession) -> Generator[TestClient, None, None]:
    async def override_get_db():
        yield db_session

    clear_orchestrator_cache()
    clear_test_api_keys()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    clear_orchestrator_cache()
    clear_test_api_keys()


@pytest.fixture
def auth_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
    register_test_api_key("tenant_a", "secret-a")
    register_test_api_key("tenant_b", "secret-b")


class TestAuthDefaultOff:
    def test_feedback_mark_without_key_when_auth_disabled(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.config.AUTH_ENABLED", False)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", False)
        response = client.post(
            "/api/feedback/mark",
            json={
                "customer_id": "tenant_a",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_200_OK


class TestAuthEnabled:
    def test_feedback_mark_401_without_key(
        self, client: TestClient, auth_enabled: None
    ) -> None:
        response = client.post(
            "/api/feedback/mark",
            json={
                "customer_id": "tenant_a",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_feedback_mark_401_invalid_key(
        self, client: TestClient, auth_enabled: None
    ) -> None:
        response = client.post(
            "/api/feedback/mark",
            headers={"X-API-Key": "wrong-key"},
            json={
                "customer_id": "tenant_a",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_feedback_mark_403_cross_tenant(
        self, client: TestClient, auth_enabled: None
    ) -> None:
        response = client.post(
            "/api/feedback/mark",
            headers={"X-API-Key": "secret-a"},
            json={
                "customer_id": "tenant_b",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_feedback_mark_200_valid_key(
        self, client: TestClient, auth_enabled: None
    ) -> None:
        response = client.post(
            "/api/feedback/mark",
            headers={"X-API-Key": "secret-a"},
            json={
                "customer_id": "tenant_a",
                "incident_id": "inc_1",
                "is_true_anomaly": True,
                "operator_id": "op_1",
            },
        )
        assert response.status_code == status.HTTP_200_OK


@pytest.mark.asyncio
async def test_learning_state_survives_cache_clear(
    db_session: AsyncSession,
) -> None:
    """Feedback persisted to DB is restored after in-memory cache eviction."""
    customer_id = "persist_cust"
    orch = await get_orchestrator(customer_id, db_session)
    orch.observe(
        incident_id="inc_persist",
        is_true_anomaly=True,
        operator_id="op_persist",
        pattern_type_observed="drift",
    )
    from src.api.services.orchestrator_registry import save_orchestrator

    await save_orchestrator(customer_id, orch, db_session)
    clear_orchestrator_cache()

    restored = await get_orchestrator(customer_id, db_session)
    stats = restored.learner.get_statistics()
    assert stats["total_feedback"] == 1
    assert stats["operator_count"] == 1

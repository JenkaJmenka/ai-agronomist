"""API tests for model activate/delete endpoints."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.api.main import app
from src.db.models import Base
from src.db.session import engine
from src.models.model_registry import ModelRegistry
from src.db.session import AsyncSessionLocal


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest_asyncio.fixture(autouse=True)
async def setup_database():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_activate_model_endpoint(client: TestClient):
    async with AsyncSessionLocal() as db:
        registry = ModelRegistry(db)
        m1 = await registry.register_model("cust_a", "cold_start", "path/v1.pt")
        m2 = await registry.register_model("cust_a", "cold_start", "path/v2.pt")

    response = client.post(
        "/api/models/cust_a/activate",
        json={"model_id": m1},
    )
    assert response.status_code == 200
    active = client.get("/api/models/cust_a")
    assert active.json()["model_id"] == m1
    assert m2  # registered second version still exists


@pytest.mark.asyncio
async def test_delete_model_version_endpoint(client: TestClient):
    async with AsyncSessionLocal() as db:
        registry = ModelRegistry(db)
        m1 = await registry.register_model("cust_b", "cold_start", "path/v1.pt")
        await registry.register_model("cust_b", "cold_start", "path/v2.pt")

    response = client.delete(f"/api/models/cust_b/versions/{m1}")
    assert response.status_code == 200
    versions = client.get("/api/models/cust_b/versions").json()
    assert len(versions) == 1
    assert versions[0]["model_id"] != m1


@pytest.mark.asyncio
async def test_delete_all_model_versions_endpoint(client: TestClient):
    async with AsyncSessionLocal() as db:
        registry = ModelRegistry(db)
        await registry.register_model("cust_c", "cold_start", "path/v1.pt")
        await registry.register_model("cust_c", "cold_start", "path/v2.pt")

    response = client.delete("/api/models/cust_c/versions")
    assert response.status_code == 200
    assert response.json()["deleted_versions"] == 2
    assert client.get("/api/models/cust_c/versions").json() == []

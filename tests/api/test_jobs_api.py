"""Tests for the /api/jobs polling endpoints."""

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.jobs import create_job, increment_progress, set_job_result
from src.db.models import Base
from src.db.session import get_db


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


@pytest.mark.asyncio
async def test_job_status_progress_and_result(test_client, test_database):
    job = await create_job(
        test_database,
        tenant_id="tenant_1",
        job_type="upload_csv",
        total_portions=4,
    )
    await increment_progress(test_database, job.id)

    response = test_client.get(f"/api/jobs/{job.id}")
    assert response.status_code == status.HTTP_200_OK
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["completed_portions"] == 1
    assert payload["progress_pct"] == pytest.approx(25.0)

    await set_job_result(
        test_database, job.id, status="completed", result={"anomalies_detected": 3}
    )
    payload = test_client.get(f"/api/jobs/{job.id}").json()
    assert payload["status"] == "completed"
    assert payload["result"] == {"anomalies_detected": 3}
    assert payload["finished_at"] is not None


def test_job_status_404_for_unknown_id(test_client):
    response = test_client.get("/api/jobs/does-not-exist")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_job_list_filters_by_tenant(test_client, test_database):
    await create_job(test_database, tenant_id="tenant_1", job_type="upload_csv")
    await create_job(test_database, tenant_id="tenant_2", job_type="upload_csv")

    response = test_client.get("/api/jobs", params={"tenant_id": "tenant_1"})
    assert response.status_code == status.HTTP_200_OK
    jobs = response.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["tenant_id"] == "tenant_1"

@pytest.mark.asyncio
async def test_job_list_filters_by_greenhouse(test_client, test_database):
    await create_job(
        test_database, tenant_id="tenant_gh", job_type="upload_csv", greenhouse_id="gh_A"
    )
    await create_job(
        test_database, tenant_id="tenant_gh", job_type="upload_csv", greenhouse_id="gh_B"
    )

    response = test_client.get(
        "/api/jobs", params={"tenant_id": "tenant_gh", "greenhouse_id": "gh_A"}
    )
    assert response.status_code == status.HTTP_200_OK
    jobs = response.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["greenhouse_id"] == "gh_A"

"""Tests for async CSV upload queue endpoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.services import upload_async_runner
from src.db.jobs import create_job, fail_stale_preparing_jobs, get_job
from src.db.models import Base
from src.db.session import AsyncSessionLocal, engine

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse_connext.csv"


@pytest.fixture
def client():
    return TestClient(app)


@pytest_asyncio.fixture
async def database_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def test_upload_async_returns_202_before_background_finishes(client: TestClient):
    """Upload-async must return 202 immediately; work continues in background."""
    calls: list[dict] = []

    async def record_pipeline(**kwargs):
        calls.append(kwargs)

    with patch(
        "src.api.routes.inference.run_upload_async_pipeline",
        side_effect=record_pipeline,
    ):
        with FIXTURE.open("rb") as fh:
            resp = client.post(
                "/api/inference/upload-async",
                data={
                    "tenant_id": "tenant_async",
                    "greenhouse_id": "default",
                    "persist_data": "false",
                    "train_model": "false",
                },
                files={"file": ("greenhouse_connext.csv", fh, "text/csv")},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["job_id"]
    assert body["portion_count"] == 0
    assert body["poll_url"] == f"/api/jobs/{body['job_id']}"
    assert len(calls) == 1
    assert calls[0]["job_id"] == body["job_id"]


@pytest.mark.asyncio
async def test_pipeline_marks_job_failed_on_checkpoint_error(
    database_schema, tmp_path, monkeypatch
):
    """Run the real pipeline body: a checkpoint-resolution error fails the job
    and removes the temp file (also guards the resolve_upload_checkpoint name)."""

    async def failing_resolve(db, tenant_id, path, train_model):
        raise ValueError("Uploaded CSV contains no valid data.")

    monkeypatch.setattr(
        upload_async_runner, "resolve_upload_checkpoint", failing_resolve
    )

    csv_path = tmp_path / "upload.csv"
    csv_path.write_text("timestamp,temperature\n")

    async with AsyncSessionLocal() as db:
        job = await create_job(
            db, tenant_id="tenant_async", job_type="upload_csv", status="preparing"
        )
        job_id = job.id

    await upload_async_runner.run_upload_async_pipeline(
        job_id=job_id,
        tmp_path=str(csv_path),
        tenant_id="tenant_async",
        greenhouse_id="default",
        persist_data=False,
        filename="upload.csv",
        train_model=False,
    )

    async with AsyncSessionLocal() as db:
        failed_job = await get_job(db, job_id)
    assert failed_job.status == "failed"
    assert "no valid data" in (failed_job.error or "")
    assert not csv_path.exists()


@pytest.mark.asyncio
async def test_startup_sweep_fails_orphaned_preparing_jobs(database_schema):
    """Jobs stuck in 'preparing' (background task lost to a restart) must be
    failed by the startup sweep; jobs in other states are untouched."""
    async with AsyncSessionLocal() as db:
        orphan = await create_job(
            db, tenant_id="tenant_async", job_type="upload_csv", status="preparing"
        )
        in_flight = await create_job(
            db, tenant_id="tenant_async", job_type="upload_csv", status="pending"
        )
        orphan_id, in_flight_id = orphan.id, in_flight.id

        swept_count = await fail_stale_preparing_jobs(db)

    assert swept_count == 1
    async with AsyncSessionLocal() as db:
        swept_job = await get_job(db, orphan_id)
        untouched_job = await get_job(db, in_flight_id)
    assert swept_job.status == "failed"
    assert "re-upload" in (swept_job.error or "")
    assert swept_job.finished_at is not None
    assert untouched_job.status == "pending"

"""Tests for the async CSV upload endpoint (durable worker preparation).

Preparation (cold-start training + portion publishing) now runs on the RabbitMQ
worker, not the API's in-process BackgroundTasks. The route must only: store the
CSV in the artifact store, create a ``preparing`` job, publish one
``upload.prepare`` message, and return 202 — no training on the request path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import update

from src.api.main import app
from src.config import STALE_PREPARE_MINUTES
from src.db.jobs import create_job, fail_stale_preparing_jobs, get_job
from src.db.models import Base, Job
from src.db.session import AsyncSessionLocal, engine
from src.models.artifact_store import put_upload_blob, resolve_upload_blob
from src.mq.schemas import UploadPrepareMessage
from src.mq.topology import UPLOAD_PREPARE_QUEUE

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


async def _backdate_job(job_id: str, minutes: int) -> None:
    """Age a job's updated_at so the time-based sweep considers it (or not)."""
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    async with AsyncSessionLocal() as db:
        await db.execute(update(Job).where(Job.id == job_id).values(updated_at=when))
        await db.commit()


def test_upload_async_returns_202_and_publishes_prepare(
    client: TestClient, database_schema
):
    """Upload-async stores the blob, publishes exactly one prepare message, and
    returns 202 without doing any training on the request path."""
    published: list = []

    async def record_publish(queue, message, **kwargs):
        published.append((queue, message))

    with patch(
        "src.api.routes.inference.publish_json", side_effect=record_publish
    ) as publish:
        with FIXTURE.open("rb") as fh:
            resp = client.post(
                "/api/inference/upload-async",
                data={
                    "tenant_id": "tenant_async",
                    "greenhouse_id": "gh1",
                    "persist_data": "true",
                    "train_model": "false",
                },
                files={"file": ("greenhouse_connext.csv", fh, "text/csv")},
            )

    assert resp.status_code == 202
    body = resp.json()
    job_id = body["job_id"]
    assert job_id
    assert body["portion_count"] == 0
    assert body["poll_url"] == f"/api/jobs/{job_id}"

    # Exactly one prepare message with the right routing key + fields.
    publish.assert_awaited_once()
    queue, message = published[0]
    assert queue == UPLOAD_PREPARE_QUEUE
    assert isinstance(message, UploadPrepareMessage)
    assert message.job_id == job_id
    assert message.tenant_id == "tenant_async"
    assert message.greenhouse_id == "gh1"
    assert message.persist_data is True
    assert message.train_model is False
    assert message.blob_path.startswith("db://model_artifacts/uploads/")


@pytest.mark.asyncio
async def test_route_stores_resolvable_blob_and_records_ref(
    client: TestClient, database_schema
):
    """The stored CSV must be durably resolvable, and the job payload must carry
    the blob_path so the sweep can clean it even if the message is lost."""
    with patch("src.api.routes.inference.publish_json", new=AsyncMock()):
        with FIXTURE.open("rb") as fh:
            original = fh.read()
        with FIXTURE.open("rb") as fh:
            resp = client.post(
                "/api/inference/upload-async",
                data={"tenant_id": "tenant_async", "greenhouse_id": "default"},
                files={"file": ("greenhouse_connext.csv", fh, "text/csv")},
            )
    job_id = resp.json()["job_id"]

    async with AsyncSessionLocal() as db:
        job = await get_job(db, job_id)
        assert job.status == "preparing"
        blob_path = job.payload["blob_path"]
        assert blob_path == f"db://model_artifacts/uploads/{job_id}"
        local = await resolve_upload_blob(db, blob_path)
    assert Path(local).read_bytes() == original


def test_route_returns_500_when_publish_fails(client: TestClient, database_schema):
    """A lost prepare message must surface as a 500 (client retries); the job is
    left for the time-based sweep, never silently reported as accepted."""
    with patch(
        "src.api.routes.inference.publish_json",
        new=AsyncMock(side_effect=RuntimeError("broker down")),
    ):
        with FIXTURE.open("rb") as fh:
            resp = client.post(
                "/api/inference/upload-async",
                data={"tenant_id": "tenant_async", "greenhouse_id": "default"},
                files={"file": ("greenhouse_connext.csv", fh, "text/csv")},
            )
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_stale_sweep_fails_old_prep_and_deletes_blob(database_schema, tmp_path):
    """A genuinely-lost prep job (no activity past the timeout) is failed and its
    blob deleted; a freshly-queued prep job is left untouched."""
    csv = tmp_path / "u.csv"
    csv.write_text("timestamp,temperature\n2020-01-01T00:00:00,21\n")

    async with AsyncSessionLocal() as db:
        stale = await create_job(
            db, tenant_id="t", job_type="upload_csv", status="preparing"
        )
        fresh = await create_job(
            db, tenant_id="t", job_type="upload_csv", status="preparing"
        )
        stale_id, fresh_id = stale.id, fresh.id
        blob_path = await put_upload_blob(db, f"uploads/{stale_id}", str(csv))
        from src.db.jobs import update_job

        await update_job(db, stale_id, payload={"blob_path": blob_path})

    # Age only the stale job past the cutoff.
    await _backdate_job(stale_id, STALE_PREPARE_MINUTES + 5)

    async with AsyncSessionLocal() as db:
        swept = await fail_stale_preparing_jobs(db)

    assert swept == 1
    async with AsyncSessionLocal() as db:
        stale_job = await get_job(db, stale_id)
        fresh_job = await get_job(db, fresh_id)
        assert stale_job.status == "failed"
        assert "re-upload" in (stale_job.error or "")
        assert stale_job.finished_at is not None
        assert fresh_job.status == "preparing"
        # Blob was deleted by the sweep.
        with pytest.raises(FileNotFoundError):
            await resolve_upload_blob(db, blob_path)


@pytest.mark.asyncio
async def test_stale_sweep_sweeps_orphaned_running_without_portions(database_schema):
    """A 'running' prep that crashed before stamping total_portions is lost too;
    a 'running' job that already produced portions must NOT be swept."""
    async with AsyncSessionLocal() as db:
        orphan = await create_job(
            db, tenant_id="t", job_type="upload_csv", status="running"
        )
        progressed = await create_job(
            db, tenant_id="t", job_type="upload_csv", status="running"
        )
        orphan_id, progressed_id = orphan.id, progressed.id
        from src.db.jobs import set_total_portions

        await set_total_portions(db, progressed_id, 5)

    await _backdate_job(orphan_id, STALE_PREPARE_MINUTES + 5)
    await _backdate_job(progressed_id, STALE_PREPARE_MINUTES + 5)

    async with AsyncSessionLocal() as db:
        swept = await fail_stale_preparing_jobs(db)

    assert swept == 1
    async with AsyncSessionLocal() as db:
        assert (await get_job(db, orphan_id)).status == "failed"
        assert (await get_job(db, progressed_id)).status == "running"

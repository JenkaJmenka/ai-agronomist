"""Unit tests for the durable upload-prepare worker handler.

Drives handle_upload_prepare directly (no broker), with the heavy CPU calls
(cold-start training, portion production) and the blob store mocked, to lock in
the orchestration: idempotent claim/resume, blob-cleanup lifecycle, and the
deterministic-vs-transient error split.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from src.api.services.upload_checkpoint import ColdStartTrainingError
from src.db.jobs import create_job, get_job, set_total_portions, update_job
from src.db.models import Base
from src.db.session import AsyncSessionLocal, engine
from src.models.bootstrap import InsufficientDataError
from src.mq.schemas import UploadPrepareMessage
from src.workers import upload_prepare_consumer as upc

CONSUMER = "src.workers.upload_prepare_consumer"


@pytest_asyncio.fixture
async def database_schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _msg(job_id: str) -> UploadPrepareMessage:
    return UploadPrepareMessage(
        job_id=job_id,
        tenant_id="t",
        greenhouse_id="gh",
        blob_path=f"db://model_artifacts/uploads/{job_id}",
        filename="u.csv",
        persist_data=True,
        train_model=False,
    )


async def _make_job(status: str = "preparing", payload: dict | None = None) -> str:
    async with AsyncSessionLocal() as db:
        job = await create_job(
            db, tenant_id="t", job_type="upload_csv", status=status, payload=payload
        )
        return job.id


def _patch_deps(**overrides):
    """Patch the handler's heavy/external deps; return the patchers' mocks."""
    defaults = {
        "resolve_upload_blob": AsyncMock(return_value="/tmp/u.csv"),
        "resolve_upload_checkpoint": AsyncMock(
            return_value=("db://model_artifacts/ck", True, "model1", None)
        ),
        "produce_upload_portions": AsyncMock(return_value=("job", 3)),
        "delete_upload_blob": AsyncMock(return_value=True),
        "clear_detector_cache": lambda: None,
        "CSVAdapter": _FakeAdapter,
        "_cleanup_local_file": lambda p: None,
    }
    defaults.update(overrides)
    return [patch(f"{CONSUMER}.{name}", val) for name, val in defaults.items()]


class _FakeAdapter:
    def __init__(self, *a, **k):
        pass

    def connect(self):
        pass

    def disconnect(self):
        pass


async def _run(msg, patchers):
    for p in patchers:
        p.start()
    try:
        async with AsyncSessionLocal() as db:
            await upc.handle_upload_prepare(msg, db)
    finally:
        for p in reversed(patchers):
            p.stop()


async def test_happy_path_trains_produces_and_deletes_blob(database_schema):
    job_id = await _make_job("preparing")
    resolve_ck = AsyncMock(return_value=("db://model_artifacts/ck", True, "m1", None))
    produce = AsyncMock(return_value=("job", 3))
    delete = AsyncMock(return_value=True)
    patchers = _patch_deps(
        resolve_upload_checkpoint=resolve_ck,
        produce_upload_portions=produce,
        delete_upload_blob=delete,
    )
    await _run(_msg(job_id), patchers)

    resolve_ck.assert_awaited_once()
    produce.assert_awaited_once()
    assert produce.await_args.kwargs["existing_job_id"] == job_id
    assert produce.await_args.kwargs["checkpoint_path"] == "db://model_artifacts/ck"
    delete.assert_awaited_once()  # blob deleted after portions enqueued
    # checkpoint_path persisted to payload BEFORE producing (idempotency anchor).
    async with AsyncSessionLocal() as db:
        job = await get_job(db, job_id)
    assert job.payload["checkpoint_path"] == "db://model_artifacts/ck"


async def test_finished_redelivery_is_skipped(database_schema):
    job_id = await _make_job("completed")
    resolve_blob = AsyncMock(return_value="/tmp/u.csv")
    patchers = _patch_deps(resolve_upload_blob=resolve_blob)
    await _run(_msg(job_id), patchers)
    resolve_blob.assert_not_awaited()  # never even resolved the blob


async def test_resume_reuses_checkpoint_without_retraining(database_schema):
    # Crashed mid-prep: status running, total_portions NULL, checkpoint already saved.
    job_id = await _make_job(
        "running", payload={"checkpoint_path": "db://model_artifacts/ck", "model_trained": True}
    )
    resolve_ck = AsyncMock()
    produce = AsyncMock(return_value=("job", 3))
    patchers = _patch_deps(
        resolve_upload_checkpoint=resolve_ck, produce_upload_portions=produce
    )
    await _run(_msg(job_id), patchers)

    resolve_ck.assert_not_awaited()  # did NOT retrain
    produce.assert_awaited_once()
    assert produce.await_args.kwargs["checkpoint_path"] == "db://model_artifacts/ck"


@pytest.mark.parametrize(
    "exc, expect_error_substr",
    [
        (InsufficientDataError("not enough rows"), "not enough rows"),
        (ValueError("Uploaded CSV contains no valid data."), "no valid data"),
        (ColdStartTrainingError("boom"), "Cold-start training failed"),
    ],
)
async def test_deterministic_error_fails_job_and_deletes_blob(
    database_schema, exc, expect_error_substr
):
    job_id = await _make_job("preparing")
    delete = AsyncMock(return_value=True)
    patchers = _patch_deps(
        resolve_upload_checkpoint=AsyncMock(side_effect=exc),
        delete_upload_blob=delete,
    )
    # Must NOT raise — deterministic errors are ACKed.
    await _run(_msg(job_id), patchers)

    async with AsyncSessionLocal() as db:
        job = await get_job(db, job_id)
    assert job.status == "failed"
    assert expect_error_substr in (job.error or "")
    delete.assert_awaited_once()


async def test_transient_error_propagates_and_preserves_blob(database_schema):
    job_id = await _make_job("preparing")
    delete = AsyncMock()
    patchers = _patch_deps(
        produce_upload_portions=AsyncMock(side_effect=RuntimeError("db down")),
        delete_upload_blob=delete,
    )
    with pytest.raises(RuntimeError):
        await _run(_msg(job_id), patchers)
    delete.assert_not_awaited()  # blob preserved so a retry/resume can re-read it


async def test_missing_blob_fails_job_with_reupload_message(database_schema):
    job_id = await _make_job("preparing")
    patchers = _patch_deps(
        resolve_upload_blob=AsyncMock(side_effect=FileNotFoundError("gone"))
    )
    await _run(_msg(job_id), patchers)
    async with AsyncSessionLocal() as db:
        job = await get_job(db, job_id)
    assert job.status == "failed"
    assert "re-upload" in (job.error or "")


async def test_terminal_hook_fails_job_and_deletes_blob(database_schema):
    job_id = await _make_job("running")
    delete = AsyncMock(return_value=True)
    with patch(f"{CONSUMER}.delete_upload_blob", delete):
        async with AsyncSessionLocal() as db:
            await upc._terminal_upload_prepare(db, _msg(job_id), "exhausted", 3)
        async with AsyncSessionLocal() as db:
            job = await get_job(db, job_id)
    assert job.status == "failed"
    assert "exhausted" in (job.error or "")
    delete.assert_awaited_once()

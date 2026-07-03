"""Tests for the Job/JobPortion store (idempotency, atomic progress, transitions)."""

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.jobs import (
    create_job,
    get_job,
    increment_progress,
    list_jobs,
    load_portions_in_order,
    prune_portion_results,
    record_portion_result,
    set_job_result,
    try_transition,
)
from src.db.models import Base, JobPortion


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest_asyncio.fixture
async def job(session):
    return await create_job(
        session,
        tenant_id="tenant_jobs",
        job_type="upload_csv",
        greenhouse_id="default",
        total_portions=3,
        payload={"filename": "data.csv"},
    )


async def test_create_and_get_job(session, job):
    fetched = await get_job(session, job.id)
    assert fetched is not None
    assert fetched.status == "pending"
    assert fetched.total_portions == 3
    assert fetched.payload["filename"] == "data.csv"


async def test_duplicate_portion_insert_is_ignored(session, job):
    first = await record_portion_result(
        session, job_id=job.id, portion_idx=0, status="completed", result={"scores": [0.1]}
    )
    duplicate = await record_portion_result(
        session, job_id=job.id, portion_idx=0, status="completed", result={"scores": [0.9]}
    )
    assert first is True
    assert duplicate is False

    count = await session.execute(
        select(func.count(JobPortion.id)).where(JobPortion.job_id == job.id)
    )
    assert count.scalar() == 1
    portions = await load_portions_in_order(session, job.id)
    assert portions[0].result == {"scores": [0.1]}  # original kept, redelivery ignored


async def test_increment_progress_returns_counts(session, job):
    completed, failed, total = await increment_progress(session, job.id)
    assert (completed, failed, total) == (1, 0, 3)
    completed, failed, total = await increment_progress(session, job.id, failed=True)
    assert (completed, failed, total) == (1, 1, 3)


async def test_try_transition_only_wins_once(session, job):
    assert await try_transition(session, job.id, "pending", "running") is True
    # Second claimant loses — status is no longer "pending".
    assert await try_transition(session, job.id, "pending", "running") is False
    assert await try_transition(session, job.id, "running", "finalizing") is True


async def test_set_result_and_prune(session, job):
    await record_portion_result(
        session, job_id=job.id, portion_idx=0, status="completed", result={"scores": [0.5]}
    )
    await set_job_result(
        session, job.id, status="completed", result={"anomalies_detected": 2}
    )
    fetched = await get_job(session, job.id)
    assert fetched.status == "completed"
    assert fetched.result == {"anomalies_detected": 2}
    assert fetched.finished_at is not None

    await prune_portion_results(session, job.id)
    portions = await load_portions_in_order(session, job.id)
    assert portions[0].result is None


async def test_list_jobs_filters(session, job):
    await create_job(session, tenant_id="tenant_jobs", job_type="training_retrain")
    await create_job(session, tenant_id="other_tenant", job_type="upload_csv")

    mine = await list_jobs(session, tenant_id="tenant_jobs")
    assert len(mine) == 2
    uploads = await list_jobs(session, tenant_id="tenant_jobs", job_type="upload_csv")
    assert len(uploads) == 1
    assert uploads[0].id == job.id

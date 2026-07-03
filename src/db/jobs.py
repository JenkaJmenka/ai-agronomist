"""Job/JobPortion store: progress tracking and redelivery idempotency.

All mutations are single statements (atomic increments, conditional
transitions, ON CONFLICT dedup) so concurrent workers cannot race each other
into double-counting or double-finalizing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Job, JobPortion

_DIALECT_INSERTS = {
    "postgresql": postgresql.insert,
    "sqlite": sqlite.insert,
}


async def create_job(
    db: AsyncSession,
    *,
    tenant_id: str,
    job_type: str,
    greenhouse_id: Optional[str] = None,
    total_portions: Optional[int] = None,
    payload: Optional[dict[str, Any]] = None,
) -> Job:
    job = Job(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        job_type=job_type,
        status="pending",
        total_portions=total_portions,
        payload=payload,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def get_job(db: AsyncSession, job_id: str) -> Optional[Job]:
    stmt = select(Job).where(Job.id == job_id).execution_options(populate_existing=True)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_jobs(
    db: AsyncSession,
    *,
    tenant_id: str,
    job_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[Job]:
    stmt = select(Job).where(Job.tenant_id == tenant_id)
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)
    if status:
        stmt = stmt.where(Job.status == status)
    stmt = stmt.order_by(Job.created_at.desc()).limit(limit).execution_options(populate_existing=True)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def record_portion_result(
    db: AsyncSession,
    *,
    job_id: str,
    portion_idx: int,
    status: str,
    result: Optional[dict[str, Any]] = None,
    attempt: int = 0,
) -> bool:
    """Insert a portion record; returns False when it was already recorded.

    ON CONFLICT DO NOTHING on (job_id, portion_idx) is the redelivery
    idempotency gate — a redelivered message must not double-process.
    """
    dialect_name = db.bind.dialect.name
    insert_fn = _DIALECT_INSERTS.get(dialect_name)
    if insert_fn is None:
        raise NotImplementedError(f"Unsupported dialect {dialect_name!r}")
    stmt = (
        insert_fn(JobPortion)
        .values(
            job_id=job_id,
            portion_idx=portion_idx,
            status=status,
            result=result,
            attempt=attempt,
            updated_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(index_elements=["job_id", "portion_idx"])
    )
    outcome = await db.execute(stmt)
    await db.commit()
    return bool(outcome.rowcount)


async def increment_progress(
    db: AsyncSession,
    job_id: str,
    *,
    failed: bool = False,
) -> tuple[int, int, Optional[int]]:
    """Atomically bump progress; returns (completed, failed, total_portions)."""
    table = Job.__table__
    values: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if failed:
        values["failed_portions"] = table.c.failed_portions + 1
    else:
        values["completed_portions"] = table.c.completed_portions + 1
    stmt = (
        update(table)
        .where(table.c.id == job_id)
        .values(**values)
        .returning(
            table.c.completed_portions,
            table.c.failed_portions,
            table.c.total_portions,
        )
    )
    result = await db.execute(stmt)
    await db.commit()
    row = result.one_or_none()
    if row is None:
        raise ValueError(f"Unknown job {job_id!r}")
    return int(row[0]), int(row[1]), row[2]


async def try_transition(
    db: AsyncSession,
    job_id: str,
    from_status: str,
    to_status: str,
) -> bool:
    """Race-free status transition; only one caller wins (finalize claim)."""
    table = Job.__table__
    stmt = (
        update(table)
        .where(table.c.id == job_id, table.c.status == from_status)
        .values(status=to_status, updated_at=datetime.now(timezone.utc))
    )
    result = await db.execute(stmt)
    await db.commit()
    return bool(result.rowcount)


async def set_job_result(
    db: AsyncSession,
    job_id: str,
    *,
    status: str,
    result: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    table = Job.__table__
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {"status": status, "updated_at": now}
    if status in ("completed", "failed", "partially_failed"):
        values["finished_at"] = now
    if result is not None:
        values["result"] = result
    if error is not None:
        values["error"] = error
    await db.execute(update(table).where(table.c.id == job_id).values(**values))
    await db.commit()


async def load_portions_in_order(db: AsyncSession, job_id: str) -> list[JobPortion]:
    result = await db.execute(
        select(JobPortion)
        .where(JobPortion.job_id == job_id)
        .order_by(JobPortion.portion_idx)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def prune_portion_results(db: AsyncSession, job_id: str) -> None:
    """Null out per-portion result blobs after finalize (keep the audit rows)."""
    table = JobPortion.__table__
    await db.execute(
        update(table)
        .where(table.c.job_id == job_id)
        .values(result=None, updated_at=datetime.now(timezone.utc))
    )
    await db.commit()


async def set_total_portions(
    db: AsyncSession,
    job_id: str,
    total: int,
) -> tuple[int, int]:
    """Producer sets the portion count once streaming ends; returns progress.

    The returned (completed, failed) lets the producer detect the race where
    every portion finished before the total was known, and fire finalize itself.
    """
    table = Job.__table__
    stmt = (
        update(table)
        .where(table.c.id == job_id)
        .values(total_portions=total, updated_at=datetime.now(timezone.utc))
        .returning(table.c.completed_portions, table.c.failed_portions)
    )
    result = await db.execute(stmt)
    await db.commit()
    row = result.one_or_none()
    if row is None:
        raise ValueError(f"Unknown job {job_id!r}")
    return int(row[0]), int(row[1])

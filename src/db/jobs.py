"""Job/JobPortion store: progress tracking and redelivery idempotency.

All mutations are single statements (atomic increments, conditional
transitions, ON CONFLICT dedup) so concurrent workers cannot race each other
into double-counting or double-finalizing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import and_, func, or_, select, types as sqltypes, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import FunctionElement

from src.config import STALE_PREPARE_MINUTES
from src.db.models import Job, JobPortion

logger = logging.getLogger(__name__)

# Per-row arrays stored in Job.result for upload jobs — each can hold one entry
# per processed sequence (~105k on a full greenhouse day), making the full result
# blob several MB per job.  The list endpoint must never ship these; callers that
# need the arrays fetch the single job detail (GET /api/jobs/{id}).
# NOTE: "embeddings" and "uncertainties" are excluded *before* the worker stores
# the result (see upload_consumer.py), but kept here as a belt-and-suspenders
# guard for older rows that may have been stored before that exclusion was added.
_HEAVY_RESULT_KEYS: tuple[str, ...] = (
    "anomaly_scores",
    "sequence_timestamps",
    "embeddings",
    "uncertainties",
    "high_anomalies",
    "statistical_summary",
)


class _JsonStripHeavyKeys(FunctionElement):
    """Strip heavy per-sequence arrays from Job.result at the SQL layer.

    Compiled output
    ---------------
    PostgreSQL : (result::jsonb - '{k1,k2,...}'::text[])::text
                 Casts json → jsonb (enables the array-remove operator),
                 removes the heavy keys, casts back to text so asyncpg
                 returns a plain string that SQLAlchemy's JSON result
                 processor can json.loads() into a dict.
    All others : json_remove(json_remove(…, '$.k1'), '$.k2', …)
                 (SQLite 3.9+ json_remove, chained one key at a time)

    Why text, not jsonb, for the Postgres cast
    -------------------------------------------
    asyncpg registers JSONB with binary codec and returns Python dicts
    directly — bypassing SQLAlchemy's JSON result_processor entirely.
    Casting to TEXT forces asyncpg to return a string, which
    SQLAlchemy's JSON result_processor then json.loads() into a dict.
    For NULL input both dialects correctly return NULL → Python None.

    Any key absent from the result JSON is silently ignored by both
    json_remove() (SQLite) and the jsonb - operator (PostgreSQL).
    """

    type = sqltypes.JSON()
    inherit_cache = True


@compiles(_JsonStripHeavyKeys, "postgresql")
def _pg_strip_heavy_keys(
    element: _JsonStripHeavyKeys, compiler: Any, **kw: Any
) -> str:
    (col,) = list(element.clauses)
    col_sql = compiler.process(col, **kw)
    keys_literal = "{" + ",".join(_HEAVY_RESULT_KEYS) + "}"
    return f"(({col_sql})::jsonb - '{keys_literal}'::text[])::text"


@compiles(_JsonStripHeavyKeys)  # default = SQLite and any other dialect
def _default_strip_heavy_keys(
    element: _JsonStripHeavyKeys, compiler: Any, **kw: Any
) -> str:
    (col,) = list(element.clauses)
    col_sql = compiler.process(col, **kw)
    # Chain json_remove() calls — SQLite applies them left-to-right
    for key in _HEAVY_RESULT_KEYS:
        col_sql = f"json_remove({col_sql}, '$.{key}')"
    return col_sql


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
    status: str = "pending",
) -> Job:
    job = Job(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        job_type=job_type,
        status=status,
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
    greenhouse_id: Optional[str] = None,
    job_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[Job]:
    stmt = select(Job).where(Job.tenant_id == tenant_id)
    if greenhouse_id:
        stmt = stmt.where(Job.greenhouse_id == greenhouse_id)
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)
    if status:
        stmt = stmt.where(Job.status == status)
    stmt = stmt.order_by(Job.created_at.desc()).limit(limit).execution_options(populate_existing=True)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_jobs_light(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: Optional[str] = None,
    job_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List jobs with heavy result keys stripped server-side.

    Replaces the former pattern of fetching full Job ORM objects (including the
    large payload and result blobs) and then discarding the heavy data in Python.

    Differences from list_jobs():
    - payload column is NOT fetched — it is never included in list responses.
    - result is stripped of _HEAVY_RESULT_KEYS at the SQL layer via
      _JsonStripHeavyKeys, so only the light subset is transferred.
    - Returns plain dicts rather than ORM objects (the route constructs
      JobStatusResponse directly, avoiding any lazy-load trigger).

    The detail view (get_job + GET /api/jobs/{id}) continues to load the full
    result via the standard ORM path — only the list view is optimised here.
    """
    stmt = (
        select(
            Job.id,
            Job.tenant_id,
            Job.greenhouse_id,
            Job.job_type,
            Job.status,
            Job.total_portions,
            Job.completed_portions,
            Job.failed_portions,
            # payload: deliberately omitted — never included in list responses.
            _JsonStripHeavyKeys(Job.result).label("result"),
            Job.error,
            Job.created_at,
            Job.updated_at,
            Job.finished_at,
        )
        .where(Job.tenant_id == tenant_id)
    )
    if greenhouse_id:
        stmt = stmt.where(Job.greenhouse_id == greenhouse_id)
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)
    if status:
        stmt = stmt.where(Job.status == status)
    stmt = stmt.order_by(Job.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    rows: list[dict[str, Any]] = []
    for mapping in result.mappings().all():
        row = dict(mapping)
        # Ensure result is a dict — SQLAlchemy's JSON result_processor handles
        # this automatically, but an explicit guard keeps us safe across driver
        # versions that may return raw JSON text instead of a parsed object.
        raw_result = row.get("result")
        if isinstance(raw_result, str):
            row["result"] = json.loads(raw_result)
        rows.append(row)
    return rows


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


async def fail_stale_preparing_jobs(db: AsyncSession) -> int:
    """Fail upload jobs whose durable preparation is genuinely lost.

    Preparation now runs on the RabbitMQ worker (durable), so a 'preparing'
    (or orphaned 'running' with total_portions still NULL) upload job may simply
    have an 'upload.prepare' message queued or actively in flight — the API
    redeploys independently of the worker, so it must NOT be failed eagerly.

    Only jobs with no activity for STALE_PREPARE_MINUTES — longer than the prep
    handler timeout plus retry budget — are treated as lost (message never
    delivered, or the DLQ terminal hook itself failed). Each such job's upload
    CSV blob is deleted as a storage backstop. This is now belt-and-suspenders:
    the worker's retry -> DLQ -> terminal-hook path fails broken prep promptly.

    Returns the number of jobs failed.
    """
    # Local import avoids a config/import cycle and keeps artifact_store lazy.
    from src.models.artifact_store import delete_upload_blob

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STALE_PREPARE_MINUTES)
    result = await db.execute(
        select(Job).where(
            Job.job_type == "upload_csv",
            Job.updated_at < cutoff,
            or_(
                Job.status == "preparing",
                and_(Job.status == "running", Job.total_portions.is_(None)),
            ),
        )
    )
    stale_jobs = list(result.scalars().all())
    if not stale_jobs:
        return 0

    for job in stale_jobs:
        blob_path = (job.payload or {}).get("blob_path")
        if blob_path:
            try:
                await delete_upload_blob(db, blob_path)
            except Exception:
                logger.warning(
                    "Stale-sweep blob delete failed for job %s", job.id, exc_info=True
                )
        job.status = "failed"
        job.error = (
            "Upload preparation did not complete within the expected time — "
            "please re-upload the file."
        )
        job.finished_at = now
        job.updated_at = now
    await db.commit()
    return len(stale_jobs)


async def update_job(
    db: AsyncSession,
    job_id: str,
    *,
    status: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Patch job status and/or payload (e.g. after background cold-start training)."""
    table = Job.__table__
    values: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if status is not None:
        values["status"] = status
    if payload is not None:
        values["payload"] = payload
    await db.execute(update(table).where(table.c.id == job_id).values(**values))
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

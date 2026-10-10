"""Job status endpoints — the dashboard polls these for async work progress."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.schemas.jobs import JobListResponse, JobStatusResponse
from src.config import AUTH_ENABLED, JOB_EVENTS_MAX_SECONDS, JOB_EVENTS_POLL_SECONDS
from src.db.jobs import _HEAVY_RESULT_KEYS, get_job, list_jobs_light, set_job_result
from src.db.models import Job
from src.db.session import get_db

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])

_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "partially_failed")

# _HEAVY_RESULT_KEYS is imported from src.db.jobs where it is also used by
# _JsonStripHeavyKeys to strip the same keys at the SQL layer — single source
# of truth for which keys are "heavy" and must not appear in list responses.


def _enforce_job_ownership(auth_tenant: Optional[str], job: Job) -> None:
    """Hide a cross-tenant job behind 404, never 403.

    These routes carry no ``tenant_id`` parameter of their own to bind via
    ``enforce_tenant_access`` — the job's own row is the only source of
    tenant identity, so ownership can only be checked after the fetch. A
    mismatch (or auth enabled with no resolved tenant, which is itself a
    bug) must not confirm the job's existence to a caller who doesn't own
    it, so this 404s exactly like "job not found" rather than 403ing.
    """
    if not AUTH_ENABLED:
        return
    if auth_tenant is None or job.tenant_id != auth_tenant:
        raise HTTPException(status_code=404, detail=f"Job {job.id!r} not found")


def _list_row_to_response(row: dict[str, Any]) -> JobStatusResponse:
    """Build a JobStatusResponse from a light job dict (from list_jobs_light).

    The result dict has already been stripped of _HEAVY_RESULT_KEYS at the SQL
    layer — no further lightening is needed here.  progress_pct is recomputed
    from the scalar counter columns (same formula as _to_response).
    """
    total: Optional[int] = row["total_portions"]
    completed: int = row["completed_portions"]
    failed: int = row["failed_portions"]
    status_val: str = row["status"]

    progress_pct: Optional[float] = None
    if total:
        progress_pct = round(100.0 * (completed + failed) / total, 2)
    elif status_val in _TERMINAL_STATUSES:
        progress_pct = 100.0

    return JobStatusResponse(
        id=row["id"],
        tenant_id=row["tenant_id"],
        greenhouse_id=row["greenhouse_id"],
        job_type=row["job_type"],
        status=status_val,
        total_portions=total,
        completed_portions=completed,
        failed_portions=failed,
        progress_pct=progress_pct,
        result=row["result"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


def _to_response(job: Job) -> JobStatusResponse:
    progress_pct: Optional[float] = None
    if job.total_portions:
        done = job.completed_portions + job.failed_portions
        progress_pct = round(100.0 * done / job.total_portions, 2)
    elif job.status in _TERMINAL_STATUSES:
        progress_pct = 100.0
    return JobStatusResponse(
        id=job.id,
        tenant_id=job.tenant_id,
        greenhouse_id=job.greenhouse_id,
        job_type=job.job_type,
        status=job.status,
        total_portions=job.total_portions,
        completed_portions=job.completed_portions,
        failed_portions=job.failed_portions,
        progress_pct=progress_pct,
        result=job.result,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
    )


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: Optional[str] = Depends(require_api_key),
) -> JobStatusResponse:
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    _enforce_job_ownership(auth_tenant, job)
    return _to_response(job)


def _event_payload(job: Job) -> str:
    """Serialize the SSE progress event body (migration plan G5 contract)."""
    snapshot = _to_response(job)
    return json.dumps(
        {
            "status": snapshot.status,
            "progress_pct": snapshot.progress_pct,
            "completed_portions": snapshot.completed_portions,
            "total_portions": snapshot.total_portions,
        }
    )


@router.get("/{job_id}/events")
async def stream_job_events(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: Optional[str] = Depends(require_api_key),
) -> StreamingResponse:
    """SSE job progress stream (G5) — replaces the dashboard's 5 s poll.

    Emits a `data:` event whenever status/progress changes and closes after
    the terminal event. Unchanged polls send an SSE comment as keep-alive.
    The stream also closes after JOB_EVENTS_MAX_SECONDS as a hard guard
    against zombie jobs holding connections open forever.
    """
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    _enforce_job_ownership(auth_tenant, job)

    async def event_stream() -> AsyncGenerator[str, None]:
        deadline = time.monotonic() + JOB_EVENTS_MAX_SECONDS
        last_payload: Optional[str] = None
        while True:
            # The session is held for the stream's lifetime; expire cached
            # state so each poll re-reads the row instead of the identity map.
            db.expire_all()
            current = await get_job(db, job_id)
            if current is None:
                break
            payload = _event_payload(current)
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            else:
                yield ": keep-alive\n\n"
            if current.status in _TERMINAL_STATUSES or time.monotonic() >= deadline:
                break
            await asyncio.sleep(JOB_EVENTS_POLL_SECONDS)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            # Disable proxy buffering so events flush immediately.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{job_id}/cancel", response_model=JobStatusResponse)
async def cancel_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    auth_tenant: Optional[str] = Depends(require_api_key),
) -> JobStatusResponse:
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    _enforce_job_ownership(auth_tenant, job)
    if job.status not in _TERMINAL_STATUSES:
        await set_job_result(db, job_id, status="failed", error="Job cancelled by user")
        job = await get_job(db, job_id)
    return _to_response(job)


@router.get("", response_model=JobListResponse)
async def get_jobs(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: Optional[str] = Query(default=None),
    job_type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    auth_tenant: Optional[str] = Depends(require_api_key),
) -> JobListResponse:
    """List jobs — heavy result arrays stripped server-side at the SQL layer.

    payload is never fetched (not in the list response schema).
    result is returned without _HEAVY_RESULT_KEYS (anomaly_scores,
    sequence_timestamps, high_anomalies, statistical_summary, …).
    Both exclusions happen in the SELECT, so the DB never sends the large
    blobs to Python.  The full result is available via GET /api/jobs/{id}.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    job_rows = await list_jobs_light(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        job_type=job_type,
        status=status,
        limit=limit,
    )
    return JobListResponse(jobs=[_list_row_to_response(row) for row in job_rows])

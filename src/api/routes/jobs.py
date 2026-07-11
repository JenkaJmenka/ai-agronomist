"""Job status endpoints — the dashboard polls these for async work progress."""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.jobs import JobListResponse, JobStatusResponse
from src.config import JOB_EVENTS_MAX_SECONDS, JOB_EVENTS_POLL_SECONDS
from src.db.jobs import get_job, list_jobs, set_job_result
from src.db.models import Job
from src.db.session import get_db

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])

_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "partially_failed")

# Per-row arrays a cold-run upload job stores in `result`. Each holds one entry
# per processed sequence (~105k on a full greenhouse day) — a few MB per job.
# The list endpoint must never ship these: the job-monitoring poller lists 20
# jobs every 5 s, and the recent-results fallback lists 5, so inlining them made
# each poll download tens of MB. Callers that need the scores fetch the single
# job's detail (GET /api/jobs/{id}), which keeps the full result.
_HEAVY_RESULT_KEYS: tuple[str, ...] = (
    "anomaly_scores",
    "sequence_timestamps",
    "embeddings",
    "uncertainties",
    "high_anomalies",
    "statistical_summary",
)


def _lighten_result(result: Optional[dict]) -> Optional[dict]:
    """Return *result* without the large per-sequence arrays (for list views)."""
    if not result:
        return result
    return {k: v for k, v in result.items() if k not in _HEAVY_RESULT_KEYS}


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
async def get_job_status(job_id: str, db: AsyncSession = Depends(get_db)) -> JobStatusResponse:
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
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
    job_id: str, db: AsyncSession = Depends(get_db)
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
async def cancel_job(job_id: str, db: AsyncSession = Depends(get_db)) -> JobStatusResponse:
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
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
) -> JobListResponse:
    jobs = await list_jobs(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        job_type=job_type,
        status=status,
        limit=limit,
    )
    responses = []
    for job in jobs:
        resp = _to_response(job)
        resp.result = _lighten_result(resp.result)
        responses.append(resp)
    return JobListResponse(jobs=responses)

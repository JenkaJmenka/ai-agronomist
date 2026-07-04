"""Job status endpoints — the dashboard polls these for async work progress."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.jobs import JobListResponse, JobStatusResponse
from src.db.jobs import get_job, list_jobs
from src.db.models import Job
from src.db.session import get_db

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])


def _to_response(job: Job) -> JobStatusResponse:
    progress_pct: Optional[float] = None
    if job.total_portions:
        done = job.completed_portions + job.failed_portions
        progress_pct = round(100.0 * done / job.total_portions, 2)
    elif job.status in ("completed", "failed", "partially_failed"):
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
    return JobListResponse(jobs=[_to_response(job) for job in jobs])

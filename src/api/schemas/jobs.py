"""Response schemas for async job status polling."""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class JobStatusResponse(BaseModel):
    id: str
    tenant_id: str
    greenhouse_id: Optional[str] = None
    job_type: str
    status: str
    total_portions: Optional[int] = None
    completed_portions: int = 0
    failed_portions: int = 0
    progress_pct: Optional[float] = Field(
        None, description="completed+failed over total, when total is known"
    )
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class JobListResponse(BaseModel):
    jobs: List[JobStatusResponse]


class UploadJobAcceptedResponse(BaseModel):
    """202-style acknowledgement for async uploads."""

    job_id: str
    portion_count: int
    poll_url: str


class JobAcceptedResponse(BaseModel):
    """Generic 202 acknowledgement for queued jobs."""

    job_id: str
    portion_count: int
    poll_url: str

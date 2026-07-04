"""Async CSV upload flow: queue a portioned job and poll it to completion.

Large uploads must not go through the synchronous inference endpoint — a big
file holds an API worker for minutes of CPU-bound scoring, starving every
other dashboard request (observed as ReadTimeouts). The async route returns
immediately and the queue worker does the heavy lifting.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from src.dashboard.services.api_client import DashboardApiClient

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "partially_failed"})
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_JOB_TIMEOUT_SECONDS = 1800.0


def estimate_csv_rows(file_bytes: bytes) -> int:
    """Data-row estimate from the newline count (excludes the header line)."""
    return max(0, file_bytes.count(b"\n") - 1)


def run_async_upload(
    client: DashboardApiClient,
    *,
    tenant_id: str,
    greenhouse_id: str,
    file_name: str,
    file_bytes: bytes,
    persist_data: bool,
    on_progress: Optional[Callable[[float, str], None]] = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Queue the upload and block until the job reaches a terminal status.

    Returns the final job payload (``status``, ``result``, ``error``, ...).
    Raises ``TimeoutError`` if the job does not finish within the deadline.
    """
    accepted = client.upload_csv_async(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        file_name=file_name,
        file_bytes=file_bytes,
        persist_data=persist_data,
    )
    job_id = accepted["job_id"]
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        job = client.get_job(job_id)
        if on_progress is not None:
            on_progress(job.get("progress_pct") or 0.0, job["status"])
        if job["status"] in TERMINAL_JOB_STATUSES:
            return job
        time.sleep(poll_interval_seconds)

    raise TimeoutError(
        f"Upload job {job_id} did not finish within {timeout_seconds:.0f}s"
    )

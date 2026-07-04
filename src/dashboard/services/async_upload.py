"""Async CSV upload helpers: queue a portioned job and track it across reruns.

Large uploads must not go through the synchronous inference endpoint — a big
file holds an API worker for minutes of CPU-bound scoring, starving every
other dashboard request (observed as ReadTimeouts). The async route returns
immediately and the queue worker does the heavy lifting.

Tracking must NOT block inside a single Streamlit script run: a long poll
loop dies on any rerun (user interaction, page reload) or a single transient
get_job error, losing the results of a job that succeeds server-side. The
page stores the pending job in session state and polls once per render.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "partially_failed"})
POLL_INTERVAL_SECONDS = 2.0
JOB_TIMEOUT_SECONDS = 1800.0


def estimate_csv_rows(file_bytes: bytes) -> int:
    """Data-row estimate from the newline count (excludes the header line)."""
    return max(0, file_bytes.count(b"\n") - 1)


def extract_upload_results(
    job: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Split a terminal job payload into (results, error_message).

    Backfills the embeddings/uncertainties keys the renderers expect — the
    worker's job result omits them, while sync responses carry empty lists.
    """
    results = job.get("result")
    if job.get("status") == "failed" or not results:
        error = job.get("error") or (results or {}).get("error") or "unknown error"
        return None, str(error)
    results.setdefault("embeddings", [])
    results.setdefault("uncertainties", [])
    return results, None

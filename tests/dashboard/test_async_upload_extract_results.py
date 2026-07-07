"""Regression coverage: a `partially_failed` 0-row-persist job must remain
displayable on the dashboard.

Companion to tests/workers/test_upload_finalize_persist_guard.py (which
covers the worker side that *sets* `partially_failed` + a non-null `error`)
and tests/dashboard/test_upload_status_bar_persist_guard.py (which covers the
status-bar banner). This module covers the piece in between:
`src.dashboard.services.async_upload.extract_upload_results`, which decides
whether a terminal job's results are shown at all.

The whole point of using `partially_failed` instead of `failed` for the
0-row-persist incident is that the 455 anomalies computed by the run remain
visible (with an error banner on top), rather than being hidden the way a
`failed` job's results are. `extract_upload_results` only returns `None` for
the results half of its tuple when `status == "failed"` or the results
payload itself is falsy -- `partially_failed` must not trip either branch.
"""

from __future__ import annotations

from src.dashboard.services.async_upload import (
    TERMINAL_JOB_STATUSES,
    extract_upload_results,
)


def _partially_failed_zero_row_job() -> dict:
    """Shape mirrors the stored job dict for the 0-row-persist incident:
    status=partially_failed, a populated `result` payload (the 455
    anomalies), and a non-null `error` describing the persistence failure.
    """
    return {
        "status": "partially_failed",
        "result": {
            "anomalies_detected": 455,
            "sequences_processed": 500,
            "data_persisted": True,
            "sensor_rows_uploaded": 0,
            "raw_rows_uploaded": 0,
        },
        "error": (
            "Persistence was requested but 0 rows were written to "
            "sensor_readings/raw_readings (processed 500 data rows). "
            "Check the worker service logs."
        ),
    }


def test_terminal_job_statuses_includes_partially_failed():
    assert "partially_failed" in TERMINAL_JOB_STATUSES


def test_extract_upload_results_partially_failed_still_returns_results():
    job = _partially_failed_zero_row_job()

    results, error_message = extract_upload_results(job)

    # The anomalies must remain visible -- this is the entire reason the
    # worker uses `partially_failed` rather than `failed` for this incident.
    assert results is not None
    assert results["anomalies_detected"] == 455
    assert results["sensor_rows_uploaded"] == 0
    assert results["raw_rows_uploaded"] == 0
    # extract_upload_results only ever surfaces an error message (and hides
    # results) for the `failed` branch; for a non-failed status with a
    # truthy result payload the second element is None regardless of
    # whether the job also carries a top-level `error`.
    assert error_message is None


def test_extract_upload_results_partially_failed_backfills_render_keys():
    job = _partially_failed_zero_row_job()

    results, _ = extract_upload_results(job)

    # Renderers expect these keys unconditionally (sync responses carry
    # empty lists; the worker's job result omits them entirely).
    assert results["embeddings"] == []
    assert results["uncertainties"] == []


def test_extract_upload_results_failed_status_hides_results_by_contrast():
    """Contrast case: a *true* `failed` job (not partially_failed) must hide
    its results -- this is precisely the behavior the 0-row-persist fix
    deliberately avoids by using `partially_failed` instead.
    """
    job = {
        "status": "failed",
        "result": {"anomalies_detected": 455},
        "error": "hard failure",
    }

    results, error_message = extract_upload_results(job)

    assert results is None
    assert error_message == "hard failure"

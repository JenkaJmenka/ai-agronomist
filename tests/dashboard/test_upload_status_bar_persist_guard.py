"""render_upload_status_bar must surface a loud error when a saved upload
result claims data_persisted=True but 0 rows actually landed -- the dashboard
half of the "455 anomalies, empty tables" incident fix.

Mocking pattern follows tests/dashboard/test_inference_results_clear.py:
patch the module-level `st` object rather than importing streamlit for real.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_st():
    with patch("src.dashboard.components.inference_results.st") as mocked:
        mocked.session_state = {"selected_tenant": "tenant_1", "selected_greenhouse": "gh_A"}
        status_col = MagicMock()
        clear_col = MagicMock()
        mocked.columns.return_value = (status_col, clear_col)
        yield mocked, status_col, clear_col


def _saved_inf(results: dict) -> dict:
    return {
        "tenant_id": "tenant_1",
        "greenhouse_id": "gh_A",
        "filename": "data.csv",
        "results": results,
    }


def test_render_upload_status_bar_errors_when_persisted_but_zero_rows(mock_st):
    from src.dashboard.components.inference_results import render_upload_status_bar

    _, status_col, _clear_col = mock_st
    saved = _saved_inf(
        {
            "anomalies_detected": 455,
            "data_persisted": True,
            "sensor_rows_uploaded": 0,
            "raw_rows_uploaded": 0,
        }
    )

    render_upload_status_bar(saved)

    assert status_col.error.called
    (msg,), _kwargs = status_col.error.call_args
    assert "0 rows" in msg
    assert "sensor_readings" in msg or "raw_readings" in msg


def test_render_upload_status_bar_no_error_when_rows_persisted(mock_st):
    from src.dashboard.components.inference_results import render_upload_status_bar

    _, status_col, _clear_col = mock_st
    saved = _saved_inf(
        {
            "anomalies_detected": 3,
            "data_persisted": True,
            "sensor_rows_uploaded": 100,
            "raw_rows_uploaded": 100,
        }
    )

    render_upload_status_bar(saved)

    assert not status_col.error.called


def test_render_upload_status_bar_no_error_when_not_persisted(mock_st):
    from src.dashboard.components.inference_results import render_upload_status_bar

    _, status_col, _clear_col = mock_st
    saved = _saved_inf(
        {
            "anomalies_detected": 0,
            "data_persisted": False,
            "sensor_rows_uploaded": 0,
            "raw_rows_uploaded": 0,
        }
    )

    render_upload_status_bar(saved)

    assert not status_col.error.called

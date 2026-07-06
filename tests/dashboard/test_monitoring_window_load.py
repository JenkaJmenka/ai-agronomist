"""Regression: Monitoring/Climate must re-query the selected window.

The 'only December shows' bug was caused by loading a fixed recent frame once
and filtering it in memory; picking any earlier window then returned nothing.
load_monitoring_window must forward the exact selected window to the readings
API so a March selection loads March — even when the data spans a full year.
"""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.dashboard.components.monitoring_jump import load_monitoring_window


class _SessionState(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key, value):
        self[key] = value


@pytest.fixture
def session_state(monkeypatch):
    state = _SessionState()
    monkeypatch.setattr(
        "src.dashboard.components.monitoring_jump.st.session_state",
        state,
    )
    return state


def test_march_window_queries_march_not_december(session_state, monkeypatch):
    # Data spans the whole year; the user selected a March window.
    march_start = pd.Timestamp("2026-03-01 00:00:00")
    march_end = pd.Timestamp("2026-03-08 00:00:00")
    march_rows = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-03-03 09:00:00"]),
            "temperature": [18.0],
        }
    )
    window_mock = MagicMock(return_value=march_rows)
    monkeypatch.setattr(
        "src.dashboard.components.charts.query_sensor_readings_window",
        window_mock,
    )

    df = load_monitoring_window("tenant_1", "gh_A", march_start, march_end)

    window_mock.assert_called_once_with(
        "tenant_1", "gh_A", march_start.isoformat(), march_end.isoformat()
    )
    assert df.iloc[0]["timestamp"] == pd.Timestamp("2026-03-03 09:00:00")


def test_full_year_window_is_forwarded_verbatim(session_state, monkeypatch):
    year_start = pd.Timestamp("2026-01-01 00:00:00")
    year_end = pd.Timestamp("2026-12-31 23:59:59")
    window_mock = MagicMock(return_value=pd.DataFrame())
    monkeypatch.setattr(
        "src.dashboard.components.charts.query_sensor_readings_window",
        window_mock,
    )

    load_monitoring_window("tenant_1", "gh_A", year_start, year_end)

    window_mock.assert_called_once_with(
        "tenant_1", "gh_A", year_start.isoformat(), year_end.isoformat()
    )

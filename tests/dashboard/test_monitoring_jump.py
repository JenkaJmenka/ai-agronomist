"""Tests for Monitoring page jump queue."""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.dashboard.components.monitoring_jump import (
    _MONITORING_PREFETCH_KEY,
    _PENDING_JUMP_KEY,
    anomalies_to_display_frame,
    load_monitoring_readings,
    peek_pending_monitoring_jump,
    queue_monitoring_jump,
    resolve_highlight_columns,
)


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


def test_resolve_highlight_columns_maps_greenhouse_temp():
    available = ["measured_greenhouse_temp", "outdoor_temp", "measured_relative_humidity"]
    cols = resolve_highlight_columns(
        {"column": "measured_greenhouse_temp", "detector": "statistical"},
        available,
    )
    assert cols == ["measured_greenhouse_temp"]


def test_resolve_highlight_columns_lstm_uses_all_channels():
    available = ["measured_greenhouse_temp", "measured_relative_humidity"]
    cols = resolve_highlight_columns({"detector": "lstm"}, available)
    assert cols == available


def test_anomalies_to_display_frame_formats_scores():
    frame = anomalies_to_display_frame(
        [
            {
                "timestamp": "2026-06-17T12:00:00+00:00",
                "detector": "statistical",
                "score": 0.9123,
                "z_score": 5.4,
                "agreement": True,
                "column": "measured_greenhouse_temp",
            }
        ]
    )
    assert frame.iloc[0]["score"] == "0.912"
    assert frame.iloc[0]["z_score"] == "5.4"
    assert frame.iloc[0]["agreement"] == "✓"


def test_queue_monitoring_jump_stores_prefetch_and_highlight_columns(session_state, monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.components.monitoring_jump.queue_sidebar_selection",
        MagicMock(),
    )
    source = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-06-17 08:00:00", "2026-06-17 12:00:00", "2026-06-18 09:00:00"]
            ),
            "measured_greenhouse_temp": [20.0, 21.0, 22.0],
        }
    )
    queue_monitoring_jump(
        {
            "timestamp": "2026-06-17T12:00:00",
            "detector": "statistical",
            "column": "measured_greenhouse_temp",
        },
        "tenant_1",
        "gh_A",
        available_columns=["measured_greenhouse_temp", "measured_relative_humidity"],
        source_df=source,
    )

    pending = peek_pending_monitoring_jump()
    assert pending is not None
    assert pending["highlight_columns"] == ["measured_greenhouse_temp"]

    prefetch = session_state[_MONITORING_PREFETCH_KEY]
    assert len(prefetch["df"]) == 2
    assert prefetch["tenant_id"] == "tenant_1"
    assert prefetch["greenhouse_id"] == "gh_A"


def test_load_monitoring_readings_uses_prefetch(session_state, monkeypatch):
    day_start = pd.Timestamp("2026-06-17")
    day_end = day_start + pd.Timedelta(hours=23, minutes=59, seconds=59)
    prefetch_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 12:00:00"]),
            "temperature": [22.0],
        }
    )
    session_state[_PENDING_JUMP_KEY] = {
        "tenant_id": "tenant_1",
        "greenhouse_id": "gh_A",
        "time_start": day_start,
        "time_end": day_end,
        "highlights": [],
        "highlight_columns": [],
    }
    session_state[_MONITORING_PREFETCH_KEY] = {
        "tenant_id": "tenant_1",
        "greenhouse_id": "gh_A",
        "time_start": day_start,
        "time_end": day_end,
        "data_min": pd.Timestamp("2026-06-01"),
        "data_max": pd.Timestamp("2026-06-30"),
        "df": prefetch_df,
    }

    df, data_min, data_max = load_monitoring_readings("tenant_1", "gh_A")
    assert len(df) == 1
    assert data_min == pd.Timestamp("2026-06-01")
    assert data_max == pd.Timestamp("2026-06-30")
    assert _MONITORING_PREFETCH_KEY not in session_state


def test_load_monitoring_readings_uses_window_query_when_no_prefetch(session_state, monkeypatch):
    day_start = pd.Timestamp("2026-06-17")
    day_end = day_start + pd.Timedelta(hours=23, minutes=59, seconds=59)
    session_state[_PENDING_JUMP_KEY] = {
        "tenant_id": "tenant_1",
        "greenhouse_id": "gh_A",
        "time_start": day_start,
        "time_end": day_end,
        "highlights": [],
        "highlight_columns": [],
    }

    window_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 12:00:00"]),
            "temperature": [22.0],
        }
    )
    window_mock = MagicMock(return_value=window_df)
    bounds_mock = MagicMock(return_value=("2026-06-01T00:00:00", "2026-06-30T00:00:00"))
    monkeypatch.setattr(
        "src.dashboard.components.charts.query_sensor_readings_window",
        window_mock,
    )
    monkeypatch.setattr(
        "src.dashboard.components.charts.query_sensor_timestamp_bounds",
        bounds_mock,
    )

    df, data_min, data_max = load_monitoring_readings("tenant_1", "gh_A")
    window_mock.assert_called_once_with(
        "tenant_1",
        "gh_A",
        day_start.isoformat(),
        day_end.isoformat(),
    )
    assert len(df) == 1
    assert data_min == pd.Timestamp("2026-06-01T00:00:00")
    assert data_max == pd.Timestamp("2026-06-30T00:00:00")

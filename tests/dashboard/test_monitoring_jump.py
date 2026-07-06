"""Tests for Monitoring page jump queue."""

from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.dashboard.components.monitoring_jump import (
    _MONITORING_PREFETCH_KEY,
    _PENDING_JUMP_COLUMNS_KEY,
    _PENDING_JUMP_KEY,
    anomalies_to_display_frame,
    apply_pending_jump_columns,
    apply_pending_jump_window,
    load_monitoring_window,
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


def _prefetch(time_start, time_end, df):
    return {
        "tenant_id": "tenant_1",
        "greenhouse_id": "gh_A",
        "time_start": time_start,
        "time_end": time_end,
        "data_min": pd.Timestamp("2026-06-01"),
        "data_max": pd.Timestamp("2026-06-30"),
        "df": df,
    }


def test_load_monitoring_window_uses_prefetch_when_it_contains_request(session_state):
    day_start = pd.Timestamp("2026-06-17")
    day_end = day_start + pd.Timedelta(hours=23, minutes=59, seconds=59)
    prefetch_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-06-17 06:00:00", "2026-06-17 12:00:00"]
            ),
            "temperature": [21.0, 22.0],
        }
    )
    session_state[_MONITORING_PREFETCH_KEY] = _prefetch(day_start, day_end, prefetch_df)

    df = load_monitoring_window("tenant_1", "gh_A", day_start, day_end)
    assert len(df) == 2
    # One-shot handoff: prefetch is consumed on read.
    assert _MONITORING_PREFETCH_KEY not in session_state


def test_load_monitoring_window_queries_when_no_prefetch(session_state, monkeypatch):
    day_start = pd.Timestamp("2026-06-17")
    day_end = day_start + pd.Timedelta(hours=23, minutes=59, seconds=59)
    window_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 12:00:00"]),
            "temperature": [22.0],
        }
    )
    window_mock = MagicMock(return_value=window_df)
    monkeypatch.setattr(
        "src.dashboard.components.charts.query_sensor_readings_window",
        window_mock,
    )

    df = load_monitoring_window("tenant_1", "gh_A", day_start, day_end)
    window_mock.assert_called_once_with(
        "tenant_1",
        "gh_A",
        day_start.isoformat(),
        day_end.isoformat(),
    )
    assert len(df) == 1


def test_load_monitoring_window_ignores_prefetch_that_does_not_contain_request(
    session_state, monkeypatch
):
    # Prefetch covers only the first half-day; a full-day request is not contained.
    prefetch_start = pd.Timestamp("2026-06-17 00:00:00")
    prefetch_end = pd.Timestamp("2026-06-17 12:00:00")
    prefetch_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 06:00:00"]),
            "temperature": [21.0],
        }
    )
    session_state[_MONITORING_PREFETCH_KEY] = _prefetch(
        prefetch_start, prefetch_end, prefetch_df
    )

    req_start = pd.Timestamp("2026-06-17 00:00:00")
    req_end = pd.Timestamp("2026-06-17 23:59:59")
    window_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17 18:00:00"]),
            "temperature": [25.0],
        }
    )
    window_mock = MagicMock(return_value=window_df)
    monkeypatch.setattr(
        "src.dashboard.components.charts.query_sensor_readings_window",
        window_mock,
    )

    df = load_monitoring_window("tenant_1", "gh_A", req_start, req_end)
    window_mock.assert_called_once_with(
        "tenant_1", "gh_A", req_start.isoformat(), req_end.isoformat()
    )
    assert df.iloc[0]["temperature"] == 25.0


def test_apply_pending_jump_window_defers_columns(session_state):
    day_start = pd.Timestamp("2026-06-17")
    day_end = day_start + pd.Timedelta(hours=23, minutes=59, seconds=59)
    session_state[_PENDING_JUMP_KEY] = {
        "tenant_id": "tenant_1",
        "greenhouse_id": "gh_A",
        "time_start": day_start,
        "time_end": day_end,
        "highlights": [{"detector": "statistical", "column": "temperature"}],
        "highlight_columns": ["temperature"],
    }

    applied = apply_pending_jump_window()
    assert applied is True
    assert session_state["time_start"] == day_start
    assert session_state["time_end"] == day_end
    assert session_state["date_range_mode"] == "custom"
    # Columns are not applied yet — deferred until the window data loads.
    assert _PENDING_JUMP_COLUMNS_KEY in session_state
    assert "cb_temperature" not in session_state

    apply_pending_jump_columns(["temperature", "humidity"])
    assert session_state["cb_temperature"] is True
    assert session_state["cb_humidity"] is False
    assert _PENDING_JUMP_COLUMNS_KEY not in session_state


def test_apply_pending_jump_window_returns_false_without_pending(session_state):
    assert apply_pending_jump_window() is False
    apply_pending_jump_columns(["temperature"])  # no-op, must not raise

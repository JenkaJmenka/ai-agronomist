"""Tests for anomaly page data helpers."""

import pandas as pd

from src.dashboard.services.anomalies_page_data import (
    downsample_for_chart,
    filter_anomalies,
    trim_cold_run_rows,
)


def test_downsample_for_chart_reduces_large_frames():
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=5000, freq="min"),
            "anomaly_score": [0.1] * 5000,
        }
    )
    sampled = downsample_for_chart(df, max_points=500)
    assert len(sampled) <= 500


def test_filter_anomalies_respects_threshold():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-17T12:00:00+00:00"] * 2),
            "anomaly_score": [0.5, 0.95],
        }
    )
    flagged = filter_anomalies(df, 0.8)
    assert len(flagged) == 1


def test_clear_anomaly_page_cache_import():
    from src.dashboard.services.anomalies_page_data import clear_anomaly_page_cache

    clear_anomaly_page_cache()


def test_rows_in_window_filters_by_timestamp():
    from src.dashboard.services.anomalies_page_data import rows_in_window

    rows = [
        {"timestamp": "2026-06-10T12:00:00+00:00", "score": 0.9},
        {"timestamp": "2026-06-20T12:00:00+00:00", "score": 0.8},
    ]
    start = pd.Timestamp("2026-06-15T00:00:00+00:00")
    end = pd.Timestamp("2026-06-25T23:59:59+00:00")
    kept = rows_in_window(rows, start, end)
    assert len(kept) == 1
    assert kept[0]["score"] == 0.8


def test_trim_cold_run_rows_limits_and_keeps_highest_scores():
    rows = [{"score": index / 100.0, "timestamp": f"2026-01-01T{index:02d}:00:00"} for index in range(100)]
    trimmed = trim_cold_run_rows(rows, limit=10)
    assert len(trimmed) == 10
    assert trimmed[0]["score"] >= trimmed[-1]["score"]

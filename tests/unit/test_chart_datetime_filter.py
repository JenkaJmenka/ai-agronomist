"""Tests for dashboard chart filtering helpers."""

import pandas as pd

from src.dashboard.components.charts import apply_datetime_filter


def test_apply_datetime_filter_returns_empty_when_no_rows_in_window():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-06-01 10:00:00", "2026-06-02 10:00:00"], utc=True
            ),
            "temperature": [20.0, 21.0],
        }
    )
    filtered = apply_datetime_filter(
        df,
        pd.Timestamp("2026-07-01 00:00:00", tz="UTC"),
        pd.Timestamp("2026-07-02 00:00:00", tz="UTC"),
    )
    assert filtered.empty


def test_apply_datetime_filter_keeps_rows_inside_window():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-01 10:00:00",
                    "2026-06-02 10:00:00",
                    "2026-06-03 10:00:00",
                ],
                utc=True,
            ),
            "temperature": [20.0, 21.0, 22.0],
        }
    )
    filtered = apply_datetime_filter(
        df,
        pd.Timestamp("2026-06-02 00:00:00", tz="UTC"),
        pd.Timestamp("2026-06-02 23:59:59", tz="UTC"),
    )
    assert len(filtered) == 1
    assert filtered.iloc[0]["temperature"] == 21.0

"""Timezone robustness for the dashboard date filters.

A tz-aware timestamp Series compared against a tz-naive bound silently drops
every row. Both apply_date_filter and apply_datetime_filter must align the two
sides first so a valid range never returns empty for a tz reason.
"""

from datetime import date, datetime

import pandas as pd

from src.dashboard.components.charts import apply_date_filter, apply_datetime_filter


def _aware_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-03-03T00:00:00+00:00",
                    "2026-03-03T12:00:00+00:00",
                    "2026-03-04T00:00:00+00:00",
                ]
            ),
            "temperature": [18.0, 19.0, 20.0],
        }
    )


def test_apply_datetime_filter_aware_data_naive_bounds_keeps_rows():
    df = _aware_frame()
    out = apply_datetime_filter(
        df,
        pd.Timestamp("2026-03-03 00:00:00"),  # naive
        pd.Timestamp("2026-03-03 23:59:59"),  # naive
    )
    assert len(out) == 2  # both March-3 rows, not silently dropped


def test_apply_date_filter_aware_data_naive_date_bounds_keeps_rows():
    df = _aware_frame()
    out = apply_date_filter(df, date(2026, 3, 3), date(2026, 3, 3), show_warning=False)
    assert len(out) == 2  # date-only end extended to 23:59:59, both rows kept


def test_apply_date_filter_datetime_bounds_preserve_time():
    df = _aware_frame()
    out = apply_date_filter(
        df,
        datetime(2026, 3, 3, 6, 0, 0),
        datetime(2026, 3, 3, 18, 0, 0),
        show_warning=False,
    )
    assert len(out) == 1  # only the 12:00 row falls inside 06:00–18:00


def test_apply_date_filter_empty_range_falls_back_to_all(monkeypatch):
    # Preserve the contract review_queue relies on: empty match → all rows.
    monkeypatch.setattr("src.dashboard.components.charts.st.warning", lambda *a, **k: None)
    df = _aware_frame()
    out = apply_date_filter(df, date(2020, 1, 1), date(2020, 1, 2), show_warning=False)
    assert len(out) == len(df)

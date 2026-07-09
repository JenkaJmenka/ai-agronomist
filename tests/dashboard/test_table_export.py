"""Tests for table export helpers."""

import pandas as pd

from src.dashboard.components.table_export import df_to_csv_bytes


def test_df_to_csv_bytes_round_trip():
    df = pd.DataFrame([{"a": 1, "b": "x"}])
    csv = df_to_csv_bytes(df).decode("utf-8")
    assert "a,b" in csv
    assert "1,x" in csv

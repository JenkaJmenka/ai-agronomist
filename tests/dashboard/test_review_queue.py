"""Tests for review queue sequence building."""

import pandas as pd

from src.dashboard.services.review_queue import build_sequence_from_readings


def test_build_sequence_from_raw_column_aliases():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [f"2026-06-01 10:{m:02d}:00" for m in range(20)]
            ),
            "measured_greenhouse_temp": [20.0 + i * 0.1 for i in range(20)],
            "measured_relative_humidity": [60.0] * 20,
            "co2": [400.0] * 20,
            "illuminance": [500.0] * 20,
        }
    )
    seq = build_sequence_from_readings(
        df, pd.Timestamp("2026-06-01 10:19:00")
    )
    assert len(seq) == 20
    assert len(seq[0]) == 4
    assert seq[-1][0] == 21.9

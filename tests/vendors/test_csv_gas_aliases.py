"""Unit tests for CSV gas / climate-extra column alias remapping."""

from __future__ import annotations

import pandas as pd

from src.vendors.csv_adapter import parse_raw_rows


def test_parse_raw_rows_remaps_gas_aliases() -> None:
    chunk = pd.DataFrame(
        {
            "timestamp": ["2026-01-01 00:00:00"],
            "temperature": [22.0],
            "humidity": [65.0],
            "measured_c2h4": [0.04],
            "measured_no": [0.01],
            "measured_no2": [0.02],
            "measured_nox": [0.03],
            "measured_co": [0.5],
            "gas_co2": [900.0],
            "co2": [420.0],
        }
    )
    result, cols = parse_raw_rows(chunk)
    assert "c2h4" in cols
    assert "no" in cols
    assert "no2" in cols
    assert "nox" in cols
    assert "co" in cols
    assert "gas_co2" in cols
    assert "co2" in cols
    assert float(result["c2h4"].iloc[0]) == 0.04
    assert float(result["co2"].iloc[0]) == 420.0
    assert float(result["gas_co2"].iloc[0]) == 900.0
    # Plain climate CO₂ must never become gas_co2.
    assert "measured_c2h4" not in cols

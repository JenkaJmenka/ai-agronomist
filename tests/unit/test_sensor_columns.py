"""Tests for sensor column classification."""

import pandas as pd

from src.dashboard.components.sensor_columns import (
    get_all_sensor_columns,
    get_climate_columns,
    get_gas_columns,
    is_climate_column,
    is_gas_column,
)


def test_climate_columns_from_greenhouse_csv_shape():
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="5min"),
            "measured_greenhouse_temp": [17.0, 17.5, 18.0],
            "measured_relative_humidity": [78.0, 77.0, 76.0],
            "light_intensity": [0.0, 10.0, 20.0],
            "wind_speed": [3.5, 3.6, 3.4],
            "co2": [400.0, 400.0, 400.0],
            "c2h4": [0.1, 0.2, 0.1],
        }
    )
    climate = get_climate_columns(df)
    assert "measured_greenhouse_temp" in climate
    assert "measured_relative_humidity" in climate
    assert "light_intensity" in climate
    assert "wind_speed" in climate
    assert "co2" not in climate
    assert "c2h4" not in climate


def test_gas_columns_detected():
    df = pd.DataFrame(
        {
            "measured_no2": [1.0, 2.0],
            "measured_co": [0.5, 0.6],
            "measured_greenhouse_temp": [20.0, 21.0],
        }
    )
    gas = get_gas_columns(df)
    assert "measured_no2" in gas
    assert "measured_co" in gas
    assert "measured_greenhouse_temp" not in gas


def test_all_sensor_columns_includes_both_domains():
    df = pd.DataFrame(
        {
            "measured_greenhouse_temp": [20.0],
            "measured_no2": [1.0],
        }
    )
    assert len(get_all_sensor_columns(df)) == 2


def test_is_climate_and_gas_helpers():
    assert is_climate_column("measured_pipe_1_temp")
    assert is_gas_column("measured_nox")
    assert not is_gas_column("measured_greenhouse_temp")

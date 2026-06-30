import pandas as pd

from src.dashboard.components.sensor_columns import (
    get_climate_columns,
    get_gas_columns,
    is_gas_column,
)


def test_is_gas_column():
    assert is_gas_column("no2")
    assert is_gas_column("Measured_NO2")
    assert is_gas_column("gas_co2")
    assert not is_gas_column("temperature")


def test_get_climate_columns_excludes_gas():
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=3, freq="h"),
        "temperature": [20.0, 21.0, 22.0],
        "no2": [1.0, 2.0, 3.0],
        "measured_voc": [0.1, 0.2, 0.3],
    })
    climate = get_climate_columns(df)
    assert "temperature" in climate
    assert "no2" not in climate
    assert "measured_voc" not in climate


def test_get_gas_columns():
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=2, freq="h"),
        "humidity": [50.0, 55.0],
        "co": [0.2, 0.3],
    })
    gas = get_gas_columns(df)
    assert gas == ["co"]

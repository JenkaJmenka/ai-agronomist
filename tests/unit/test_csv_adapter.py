import os
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from src.vendors.csv_adapter import CSVAdapter
from src.vendors.base import (
    DataSourceConnectionError,
    DataReadError,
    NormalizationError,
    DataValidationError
)


@pytest.fixture
def sample_csv_data(tmp_path):
    """Creates a temporary valid CSV file with standard sensor readings."""
    filepath = tmp_path / "valid_sensor_data.csv"
    data = {
        "raw_time": ["2026-06-09 12:00:00", "2026-06-09 13:00:00", "2026-06-09 14:00:00"],
        "temp_raw": [22.5, 23.1, 21.9],
        "humid_raw": [60.2, 58.5, 62.0],
        "co2_raw": [410.0, 395.0, 420.0],
        "light_raw": [850.0, 900.0, 780.0]
    }
    df = pd.DataFrame(data)
    df.to_csv(filepath, index=False)
    return str(filepath)


@pytest.fixture
def sample_csv_invalid_types_data(tmp_path):
    """Creates a temporary CSV file with invalid non-numeric types."""
    filepath = tmp_path / "invalid_type_sensor_data.csv"
    data = {
        "raw_time": ["2026-06-09 12:00:00", "2026-06-09 13:00:00", "2026-06-09 14:00:00"],
        "temp_raw": ["hot", 23.1, 21.9],  # invalid type
        "humid_raw": [60.2, 58.5, 62.0],
        "co2_raw": [410.0, 395.0, 420.0],
        "light_raw": [850.0, 900.0, 780.0]
    }
    df = pd.DataFrame(data)
    df.to_csv(filepath, index=False)
    return str(filepath)


@pytest.fixture
def sample_csv_out_of_bounds_data(tmp_path):
    """Creates a temporary CSV file with out of physical bounds values."""
    filepath = tmp_path / "out_of_bounds_sensor_data.csv"
    data = {
        "raw_time": ["2026-06-09 12:00:00", "2026-06-09 13:00:00", "2026-06-09 14:00:00"],
        "temp_raw": [120.5, 23.1, 21.9],  # out of bounds (allowed range: 0.0 to 50.0)
        "humid_raw": [60.2, 58.5, 62.0],
        "co2_raw": [410.0, 395.0, 420.0],
        "light_raw": [850.0, 900.0, 780.0]
    }
    df = pd.DataFrame(data)
    df.to_csv(filepath, index=False)
    return str(filepath)


@pytest.fixture
def sample_csv_nan_data(tmp_path):
    """Creates a temporary CSV file containing NaN values."""
    filepath = tmp_path / "nan_sensor_data.csv"
    data = {
        "raw_time": ["2026-06-09 12:00:00", "2026-06-09 13:00:00", "2026-06-09 14:00:00"],
        "temp_raw": [22.5, np.nan, 21.9],  # NaN value
        "humid_raw": [60.2, 58.5, 62.0],
        "co2_raw": [410.0, 395.0, 420.0],
        "light_raw": [850.0, 900.0, 780.0]
    }
    df = pd.DataFrame(data)
    df.to_csv(filepath, index=False)
    return str(filepath)


@pytest.fixture
def default_column_mapping():
    """Returns valid column mapping configuration."""
    return {
        "raw_time": "timestamp",
        "temp_raw": "temperature",
        "humid_raw": "humidity",
        "co2_raw": "co2",
        "light_raw": "light"
    }


def test_csv_adapter_init():
    """Tests proper initialization and default state."""
    config = {"filepath": "dummy.csv"}
    adapter = CSVAdapter(config)
    assert adapter.config == config
    assert adapter.name == "csv"
    assert adapter._connected is False


def test_connect_success(sample_csv_data):
    """Tests connection verification with an existing CSV file."""
    config = {"filepath": sample_csv_data}
    adapter = CSVAdapter(config)
    adapter.connect()
    assert adapter._connected is True


def test_connect_fails_missing_filepath():
    """Tests connection failure when filepath parameter is missing."""
    adapter = CSVAdapter({})
    with pytest.raises(DataSourceConnectionError, match="Filepath not specified in config."):
        adapter.connect()


def test_connect_fails_file_not_found():
    """Tests connection failure when CSV file does not exist on disk."""
    adapter = CSVAdapter({"filepath": "does_not_exist_file_path.csv"})
    with pytest.raises(DataSourceConnectionError, match="CSV file not found at path:"):
        adapter.connect()


def test_disconnect():
    """Tests disconnect resets connected status."""
    adapter = CSVAdapter({"filepath": "dummy.csv"})
    adapter._connected = True
    adapter.disconnect()
    assert adapter._connected is False


def test_read_success(sample_csv_data):
    """Tests reading CSV data into raw DataFrame when connected."""
    adapter = CSVAdapter({"filepath": sample_csv_data})
    adapter.connect()
    df = adapter.read()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 3
    assert list(df.columns) == ["raw_time", "temp_raw", "humid_raw", "co2_raw", "light_raw"]


def test_read_without_connection():
    """Tests read failure if connect() was not called first."""
    adapter = CSVAdapter({"filepath": "dummy.csv"})
    with pytest.raises(DataReadError, match="Adapter is not connected."):
        adapter.read()


def test_read_missing_filepath_after_connection_state():
    """Tests read failure if filepath is not configured."""
    adapter = CSVAdapter({"filepath": ""})
    adapter._connected = True
    with pytest.raises(DataReadError, match="Filepath not specified in config."):
        adapter.read()


def test_read_malformed_csv_raises_read_error(tmp_path):
    """Tests raw-read failure when file content is not a parseable CSV."""
    from unittest.mock import patch
    filepath = tmp_path / "corrupt.csv"
    with open(filepath, "w") as f:
        f.write("dummy")
    
    adapter = CSVAdapter({"filepath": str(filepath)})
    adapter.connect()
    with patch("pandas.read_csv", side_effect=Exception("corrupted file")):
        with pytest.raises(DataReadError, match="Failed to read raw CSV data"):
            adapter.read()


def test_normalize_success(sample_csv_data, default_column_mapping):
    """Tests successful mapping, timezone converson and numeric coercion."""
    adapter = CSVAdapter({
        "filepath": sample_csv_data,
        "column_mapping": default_column_mapping
    })
    adapter.connect()
    raw_df = adapter.read()
    normalized_df = adapter.normalize(raw_df)

    assert list(normalized_df.columns) == ["timestamp", "temperature", "humidity", "co2", "light"]
    assert normalized_df["timestamp"].dt.tz == timezone.utc
    assert normalized_df["temperature"].dtype == "float64"
    assert normalized_df["humidity"].dtype == "float64"
    assert normalized_df["co2"].dtype == "float64"
    assert normalized_df["light"].dtype == "float64"
    assert normalized_df["temperature"].iloc[0] == 22.5


def test_normalize_raises_error_if_none_input(default_column_mapping):
    """Tests that normalizing a None-type raises NormalizationError."""
    adapter = CSVAdapter({"column_mapping": default_column_mapping})
    with pytest.raises(NormalizationError, match="Cannot normalize a None DataFrame."):
        adapter.normalize(None)  # type: ignore


def test_normalize_missing_columns_after_mapping(sample_csv_data):
    """Tests normalization error if the rename map leaves required columns missing."""
    adapter = CSVAdapter({
        "filepath": sample_csv_data,
        "column_mapping": {"raw_time": "timestamp"}  # missing temp, humidity, etc.
    })
    adapter.connect()
    raw_df = adapter.read()
    with pytest.raises(NormalizationError, match="missing required columns"):
        adapter.normalize(raw_df)


def test_normalize_invalid_timestamp_raises_normalization_error(tmp_path, default_column_mapping):
    """Tests that unparseable timestamps trigger NormalizationError."""
    filepath = tmp_path / "bad_time.csv"
    df = pd.DataFrame({
        "raw_time": ["not-a-datetime", "2026-06-09 13:00:00", "2026-06-09 14:00:00"],
        "temp_raw": [22.5, 23.1, 21.9],
        "humid_raw": [60.2, 58.5, 62.0],
        "co2_raw": [410.0, 395.0, 420.0],
        "light_raw": [850.0, 900.0, 780.0]
    })
    df.to_csv(filepath, index=False)
    
    adapter = CSVAdapter({
        "filepath": str(filepath),
        "column_mapping": default_column_mapping
    })
    adapter.connect()
    raw_df = adapter.read()
    
    with pytest.raises(NormalizationError, match="Failed to normalize 'timestamp' column"):
        adapter.normalize(raw_df)


def test_normalize_invalid_numeric_types_raises_normalization_error(sample_csv_invalid_types_data, default_column_mapping):
    """Tests that uncoercible non-numeric properties trigger NormalizationError."""
    adapter = CSVAdapter({
        "filepath": sample_csv_invalid_types_data,
        "column_mapping": default_column_mapping
    })
    adapter.connect()
    raw_df = adapter.read()
    
    with pytest.raises(NormalizationError, match="Failed to coerce column 'temperature' to float64"):
        adapter.normalize(raw_df)


def test_get_data_lifecycle_integration(sample_csv_data, default_column_mapping):
    """Tests the standard orchestrator get_data lifecycle from connection to validated results."""
    adapter = CSVAdapter({
        "filepath": sample_csv_data,
        "column_mapping": default_column_mapping
    })
    df = adapter.get_data()
    assert list(df.columns) == ["timestamp", "temperature", "humidity", "co2", "light"]
    assert len(df) == 3


def test_validate_success(sample_csv_data, default_column_mapping):
    """Tests that validated standard correct CSV data succeeds."""
    adapter = CSVAdapter({
        "filepath": sample_csv_data,
        "column_mapping": default_column_mapping
    })
    df = adapter.get_data()
    assert adapter.validate(df) is True


def test_normalize_drops_nan_sensor_rows(sample_csv_nan_data, default_column_mapping):
    """Rows where primary sensors are NaN are silently dropped during normalize."""
    adapter = CSVAdapter({
        "filepath": sample_csv_nan_data,
        "column_mapping": default_column_mapping
    })
    # Should not raise — NaN temperature row is dropped, not propagated.
    result = adapter.get_data()
    # Original fixture has 3 rows; 1 row has NaN temperature → 2 rows remain.
    assert len(result) == 2
    assert result["temperature"].isna().sum() == 0


def test_validate_out_of_bounds_raises_error(sample_csv_out_of_bounds_data, default_column_mapping):
    """Tests that readings outside physical bounds trigger DataValidationError."""
    adapter = CSVAdapter({
        "filepath": sample_csv_out_of_bounds_data,
        "column_mapping": default_column_mapping
    })
    with pytest.raises(DataValidationError, match="Column 'temperature' contains values above allowed maximum"):
        adapter.get_data()


# ---------------------------------------------------------------------------
# Measured HD column detection (climate-extras, not a model input)
# ---------------------------------------------------------------------------


class TestMeasuredHDColumnMapping:
    """Verify that 'Humidity deficit g/m3' maps to measured_hd, NOT humidity."""

    def test_humidity_deficit_maps_to_measured_hd(self) -> None:
        """A column named 'Humidity deficit g/m3' maps to measured_hd."""
        from src.vendors.csv_adapter import _smart_column_map
        cols = ["timestamp", "temperature", "Relative Humidity %", "Humidity deficit g/m3", "co2", "light"]
        mapping = _smart_column_map(cols)
        assert mapping.get("Humidity deficit g/m3") == "measured_hd"

    def test_humidity_deficit_does_not_map_to_humidity(self) -> None:
        """'Humidity deficit g/m3' must NOT steal the humidity channel."""
        from src.vendors.csv_adapter import _smart_column_map
        cols = ["timestamp", "temperature", "Relative Humidity %", "Humidity deficit g/m3", "co2", "light"]
        mapping = _smart_column_map(cols)
        # humidity should map from 'Relative Humidity %', not from the deficit column
        assert mapping.get("Relative Humidity %") == "humidity"

    def test_normal_humidity_still_maps_correctly(self) -> None:
        """A plain 'humidity' column still maps to the humidity channel."""
        from src.vendors.csv_adapter import _smart_column_map
        cols = ["timestamp", "temperature", "humidity", "co2", "light"]
        mapping = _smart_column_map(cols)
        assert mapping.get("humidity") == "humidity"

    def test_measured_hd_not_in_sensor_cols(self) -> None:
        """measured_hd must not appear in SENSOR_COLS (model stays 4-dim)."""
        from src.vendors.csv_adapter import SENSOR_COLS
        assert "measured_hd" not in SENSOR_COLS

    def test_hd_exact_maps_to_measured_hd(self) -> None:
        """An exact 'hd' column header maps to measured_hd (low score)."""
        from src.vendors.csv_adapter import _smart_column_map
        cols = ["timestamp", "temperature", "humidity", "co2", "light", "hd"]
        mapping = _smart_column_map(cols)
        assert mapping.get("hd") == "measured_hd"

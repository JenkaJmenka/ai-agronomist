import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from src.vendors.base import (
    DataSourceAdapter,
    VendorError,
    DataSourceConnectionError,
    DataReadError,
    NormalizationError,
    DataValidationError,
    AbstractVendorAdapter
)

# =============================================================================
# Mock Implementation for Abstract Class Testing
# =============================================================================

class MockAdapter(DataSourceAdapter):
    """A mock concrete adapter implementation to test base class behavior."""
    
    def __init__(self, config=None):
        super().__init__(config)
        self.connected = False
        self.disconnected = False
        self.read_called = False
        self.normalize_called = False
        self.validate_called = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def read(self) -> pd.DataFrame:
        self.read_called = True
        # Return a simple raw DataFrame
        return pd.DataFrame({"raw_col": [1, 2, 3]})

    def normalize(self, data: pd.DataFrame) -> pd.DataFrame:
        self.normalize_called = True
        # Return canonical columns
        df = pd.DataFrame({
            "timestamp": pd.date_range("2026-06-09T10:00:00", periods=len(data), freq="h", tz="UTC"),
            "temperature": [22.5, 23.0, 21.8],
            "humidity": [60.0, 62.1, 58.5],
            "co2": [400.0, 410.0, 395.0],
            "light": [800.0, 850.0, 780.0]
        })
        return df

    def validate(self, data: pd.DataFrame) -> bool:
        self.validate_called = True
        return self.validate_canonical_schema(data)


# =============================================================================
# Unit Tests
# =============================================================================

def test_cannot_instantiate_abstract_base_class():
    """Verifies that the DataSourceAdapter ABC cannot be directly instantiated."""
    with pytest.raises(TypeError):
        DataSourceAdapter(config={})  # type: ignore


def test_alias_mapping():
    """Verifies that the design doc alias AbstractVendorAdapter resolves to DataSourceAdapter."""
    assert AbstractVendorAdapter is DataSourceAdapter


def test_concrete_adapter_instantiation_and_config():
    """Verifies that subclass receives config arguments properly."""
    config = {"credential_token": "abc_mock_token", "limit": 10}
    adapter = MockAdapter(config=config)
    assert adapter.config == config
    assert adapter.config["credential_token"] == "abc_mock_token"


def test_adapter_compatibility_methods():
    """Verifies that calling alternative methods and aliases is supported and delegates correctly."""
    adapter = MockAdapter()
    
    # Test read_data alias
    df_raw = adapter.read_data()
    assert adapter.read_called is True
    assert "raw_col" in df_raw.columns

    # Test normalize_data alias
    df_norm = adapter.normalize_data(df_raw)
    assert adapter.normalize_called is True
    assert "temperature" in df_norm.columns

    # Test validate_data alias
    adapter.validate_data(df_norm)
    assert adapter.validate_called is True


def test_get_data_lifecycle_flow():
    """Verifies that the main template method get_data executes the full pipeline correctly."""
    adapter = MockAdapter()
    df = adapter.get_data()
    
    assert adapter.connected is True
    assert adapter.read_called is True
    assert adapter.normalize_called is True
    assert adapter.validate_called is True
    assert adapter.disconnected is True
    assert list(df.columns) == ["timestamp", "temperature", "humidity", "co2", "light"]


def test_get_data_lifecycle_ensures_disconnect():
    """Verifies that disconnect is always called, even if read/normalize fails."""
    class FailingAdapter(MockAdapter):
        def read(self) -> pd.DataFrame:
            raise DataReadError("Simulated read failure")

    adapter = FailingAdapter()
    with pytest.raises(DataReadError):
        adapter.get_data()

    assert adapter.connected is True
    assert adapter.disconnected is True


def test_canonical_schema_validation_success():
    """Verifies that a valid canonical DataFrame passes validation checks."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-09T10:00:00", periods=3, freq="h", tz="UTC"),
        "temperature": [22.5, 0.0, 50.0],
        "humidity": [60.0, 0.0, 100.0],
        "co2": [400.0, 0.0, 1000.0],
        "light": [800.0, 0.0, 2000.0]
    })
    assert DataSourceAdapter.validate_canonical_schema(df) is True


def test_canonical_schema_missing_column():
    """Verifies that validator detects missing required canonical columns."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-09T10:00:00", periods=3, freq="h", tz="UTC"),
        "temperature": [22.5, 23.0, 22.0],
        "humidity": [60.0, 62.0, 61.0],
        "co2": [400.0, 410.0, 405.0]
        # Missing light column
    })
    with pytest.raises(DataValidationError, match="Missing required canonical column: 'light'"):
        DataSourceAdapter.validate_canonical_schema(df)


def test_canonical_schema_empty_dataframe():
    """Verifies that empty DataFrames are rejected."""
    df = pd.DataFrame(columns=["timestamp", "temperature", "humidity", "co2", "light"])
    with pytest.raises(DataValidationError, match="DataFrame is empty"):
        DataSourceAdapter.validate_canonical_schema(df)


def test_canonical_schema_timestamp_not_datetime():
    """Verifies that non-datetime timestamps are rejected."""
    df = pd.DataFrame({
        "timestamp": ["2026-06-09", "2026-06-10", "2026-06-11"],  # strings, not parsed
        "temperature": [22.5, 23.0, 22.0],
        "humidity": [60.0, 62.0, 61.0],
        "co2": [400.0, 410.0, 405.0],
        "light": [800.0, 810.0, 805.0]
    })
    with pytest.raises(DataValidationError, match="Column 'timestamp' must be datetime-like"):
        DataSourceAdapter.validate_canonical_schema(df)


def test_canonical_schema_naive_timestamp():
    """Verifies that timestamps that aren't timezone-aware (naive) are rejected."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-09T10:00:00", periods=3, freq="h"),  # naive
        "temperature": [22.5, 23.0, 22.0],
        "humidity": [60.0, 62.0, 61.0],
        "co2": [400.0, 410.0, 405.0],
        "light": [800.0, 810.0, 805.0]
    })
    with pytest.raises(DataValidationError, match="Column 'timestamp' must have timezone-aware UTC values"):
        DataSourceAdapter.validate_canonical_schema(df)


@pytest.mark.parametrize("col, val", [
    ("temperature", -0.1),
    ("temperature", 50.1),
    ("humidity", -0.1),
    ("humidity", 100.1),
    ("co2", -0.1),
    ("co2", 1000.1),
    ("light", -0.1),
    ("light", 2000.1),
])
def test_canonical_schema_out_of_bounds_values(col, val):
    """Verifies that any values exceeding physical bounds are caught and report details."""
    df_template = {
        "timestamp": pd.date_range("2026-06-09T10:00:00", periods=3, freq="h", tz="UTC"),
        "temperature": [22.5, 23.0, 22.0],
        "humidity": [60.0, 62.0, 61.0],
        "co2": [400.0, 410.0, 405.0],
        "light": [800.0, 810.0, 805.0]
    }
    # Overwrite one value with out-of-bounds parameter
    df_template[col][1] = val
    df = pd.DataFrame(df_template)
    
    bound_type = "below allowed minimum" if val < 0 else "above allowed maximum"
    with pytest.raises(DataValidationError, match=f"Column '{col}' contains values {bound_type}"):
        DataSourceAdapter.validate_canonical_schema(df)


def test_canonical_schema_non_numeric_types():
    """Verifies that numeric columns are validated as floats or integers."""
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-06-09T10:00:00", periods=3, freq="h", tz="UTC"),
        "temperature": ["high", "medium", "low"],  # Object/string
        "humidity": [60.0, 62.0, 61.0],
        "co2": [400.0, 410.0, 405.0],
        "light": [800.0, 810.0, 805.0]
    })
    with pytest.raises(DataValidationError, match="Column 'temperature' must be of numeric type"):
        DataSourceAdapter.validate_canonical_schema(df)

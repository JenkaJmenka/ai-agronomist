from abc import ABC, abstractmethod
from typing import Any, Dict
import pandas as pd

# =============================================================================
# Custom Exception Hierarchy
# =============================================================================

class VendorError(Exception):
    """Base exception class for all vendor adapter exceptions."""
    pass


class DataSourceConnectionError(VendorError, ConnectionError):
    """Raised when establishing connection with the data source fails."""
    pass


class DataReadError(VendorError):
    """Raised when reading raw data from the vendor source fails."""
    pass


class NormalizationError(VendorError, ValueError):
    """Raised when transforming raw vendor data to the canonical format fails."""
    pass


class DataValidationError(VendorError, ValueError):
    """Custom exception for data validation errors against canonical schema."""
    pass


# =============================================================================
# Abstract Base Class Interface
# =============================================================================

class DataSourceAdapter(ABC):
    """
    Abstract base class for all vendor sensor data adapters.

    This class defines a common contract/interface for connecting, disconnecting,
    reading, validating, and normalizing sensor data from diverse vendor platforms (e.g. Priva, CSV).
    It guarantees that any vendor-specific implementation transforms raw metrics into a canonical, multi-tenant
    schema ready for down-stream machine learning model consumption and database insertion.

    Expected Canonical Schema columns:
    - `timestamp`: UTC datetime (datetime64[ns, UTC])
    - `temperature`: float64 (Ambient temperature in degrees Celsius, [0.0, 50.0])
    - `humidity`: float64 (Relative humidity percentage, [0.0, 100.0])
    - `co2`: float64 (CO2 concentration in parts per million (ppm), [0.0, 1000.0])
    - `light`: float64 (Light intensity in lux, [0.0, 2000.0])

    Error Handling strategy is fully specified:
    - Anticipated failures (such as connection timeouts, file-not-found, schema mismatch, etc.)
      must raise specific, wrapped exceptions (e.g. `DataSourceConnectionError`, `DataReadError`, 
      `NormalizationError`, `DataValidationError`) to decouple calling APIs from vendor-specific details.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initializes the adapter with vendor-specific configuration.

        Args:
            config: A dictionary containing connection inputs, credentials,
                    file paths, mapping dictionaries, or thresholds.
        """
        self.config = config or {}

    @abstractmethod
    def connect(self) -> None:
        """
        Establishes a connection to the vendor's data source.
        This could be a remote API client, database host, or local file checker.

        Raises:
            DataSourceConnectionError: If connection cannot be established.
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """
        Gracefully terminates the connection instance and deallocates resources.
        """
        pass

    @abstractmethod
    def read(self) -> pd.DataFrame:
        """
        Reads raw data directly from the vendor's source channel.

        Returns:
            A pandas DataFrame containing raw sensor data with vendor-specific structure.

        Raises:
            DataReadError: If the raw data stream cannot be fetched or parsed.
        """
        pass

    @abstractmethod
    def normalize(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Normalizes the raw DataFrame structure to standard canonical schema formatting (renaming columns, 
        timezone conversion to UTC, casting data types).

        Args:
            data: Raw DataFrame direct from the read stream.

        Returns:
            A pandas DataFrame formatted as canonical schema.

        Raises:
            NormalizationError: If required mapping columns are absent, or type-conversion fails.
        """
        pass

    @abstractmethod
    def validate(self, data: pd.DataFrame) -> bool:
        """
        Performs data quality and schema compliance validation checks on normalized DataFrame.

        Args:
            data: Normalized DataFrame containing canonical structure.

        Returns:
            True if DataFrame passes validation checks.

        Raises:
            DataValidationError: If schema or value constraints are violated.
        """
        pass

    # =============================================================================
    # Template Methods and Adapter Aliases (Backwards Compatibility)
    # =============================================================================

    def read_data(self) -> pd.DataFrame:
        """Alternative alias for the read() method."""
        return self.read()

    def normalize_data(self, raw_data: pd.DataFrame) -> pd.DataFrame:
        """Alternative alias for the normalize() method."""
        return self.normalize(raw_data)

    def validate_data(self, df: pd.DataFrame) -> None:
        """Alternative alias for the validate() method."""
        self.validate(df)

    def get_data(self) -> pd.DataFrame:
        """
        Template orchestrator executing adapter sequence: Connect, Read, Normalize, Validate, Disconnect.

        Returns:
            A validated, normalized Pandas DataFrame conforming to the canonical schema.

        Raises:
            DataSourceConnectionError: Connection failure.
            DataReadError: Read failure.
            NormalizationError: Transformation failure.
            DataValidationError: Data quality check failing.
        """
        self.connect()
        try:
            raw_data = self.read()
            normalized_data = self.normalize(raw_data)
            self.validate(normalized_data)
            return normalized_data
        finally:
            self.disconnect()

    @staticmethod
    def validate_canonical_schema(data: pd.DataFrame) -> bool:
        """
        Utility static helper to validate a DataFrame against the canonical schema constraints.
        Can be used by any concrete adapter in its `validate` method.

        Args:
            data: The DataFrame to validate.

        Returns:
            True if valid.

        Raises:
            DataValidationError: If any validation rule is violated.
        """
        required_cols = ["timestamp", "temperature", "humidity", "co2", "light"]
        
        # 1. Check for missing columns
        for col in required_cols:
            if col not in data.columns:
                raise DataValidationError(f"Missing required canonical column: '{col}'")

        # 2. Check for empty dataframe
        if data.empty:
            raise DataValidationError("DataFrame is empty and contains no sensor readings")

        # 3. Check and validate timestamp column
        if not pd.api.types.is_datetime64_any_dtype(data["timestamp"]):
            raise DataValidationError("Column 'timestamp' must be datetime-like (type datetime64)")
            
        try:
            if getattr(data["timestamp"].dt, "tz", None) is None:
                raise DataValidationError("Column 'timestamp' must have timezone-aware UTC values")
        except AttributeError:
            raise DataValidationError("Fail to parse 'timestamp' Series as datetime or access .dt namespace")

        # 4. Check that numerical columns are numeric and within physical bounds:
        bounds = {
            "temperature": (0.0, 50.0),
            "humidity": (0.0, 100.0),
            "co2": (0.0, 1000.0),
            "light": (0.0, 2000.0)
        }

        for col, (min_val, max_val) in bounds.items():
            if not pd.api.types.is_numeric_dtype(data[col]):
                raise DataValidationError(f"Column '{col}' must be of numeric type")

            if data[col].isna().any():
                raise DataValidationError(f"Column '{col}' contains values that are NaN")

            # Check actual bounds and raise explicit DataValidationError on failure
            out_of_bounds_low = data[data[col] < min_val]
            out_of_bounds_high = data[data[col] > max_val]
            
            if not out_of_bounds_low.empty:
                val = out_of_bounds_low[col].iloc[0]
                raise DataValidationError(
                    f"Column '{col}' contains values below allowed minimum of {min_val} (found: {val})"
                )
            if not out_of_bounds_high.empty:
                val = out_of_bounds_high[col].iloc[0]
                raise DataValidationError(
                    f"Column '{col}' contains values above allowed maximum of {max_val} (found: {val})"
                )

        return True


# Alias to support AbstractVendorAdapter design naming conventions
AbstractVendorAdapter = DataSourceAdapter

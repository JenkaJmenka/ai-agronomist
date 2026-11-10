# Vendor Adapter Design

**Status:** Proposed
**Author:** Architect
**Date:** 2026-06-08

## 1. Overview

This document outlines the design for the Vendor Adapter system, a crucial component for ingesting sensor data from various greenhouse control system vendors. The primary goal is to create a pluggable architecture that allows for the easy addition of new vendors without requiring changes to the core data processing pipeline.

This is achieved by defining a standardized interface that each vendor-specific adapter must implement. This ensures that data, regardless of its source, is transformed into a canonical format that the rest of the AI Agronomist platform can understand.

## 2. Abstract Vendor Interface

All vendor adapters must inherit from an abstract base class, `AbstractVendorAdapter`, which defines the contract for data ingestion.

### `AbstractVendorAdapter` Specification

```python
from abc import ABC, abstractmethod
import pandas as pd

class AbstractVendorAdapter(ABC):
    """
    Abstract interface for all vendor-specific data adapters.
    """

    @abstractmethod
    def connect(self) -> None:
        """
        Establishes a connection to the vendor's data source.
        This could be a file path, a database connection, or an API client.
        Raises ConnectionError on failure.
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """
        Closes the connection to the data source.
        """
        pass

    @abstractmethod
    def read_data(self) -> pd.DataFrame:
        """
        Reads data from the source and returns it as a Pandas DataFrame.
        The DataFrame returned by this method is expected to have raw,
        vendor-specific column names.

        :return: A DataFrame with the raw sensor data.
        :raises: DataReadError if data cannot be read.
        """
        pass

    @abstractmethod
    def normalize_data(self, raw_data: pd.DataFrame) -> pd.DataFrame:
        """
        Transforms the raw data into the canonical format.

        :param raw_data: A DataFrame with vendor-specific columns.
        :return: A DataFrame with standardized column names and data types.
        """
        pass

    def get_data(self) -> pd.DataFrame:
        """
        A template method that orchestrates the data ingestion process.
        """
        self.connect()
        try:
            raw_data = self.read_data()
            normalized_data = self.normalize_data(raw_data)
            return normalized_data
        finally:
            self.disconnect()

```

### Input/Output Contracts

- **Input:** Each adapter is responsible for handling its specific data format (e.g., CSV file, API response, database table).
- **Output:** The `get_data` method on any implemented adapter will always return a `pandas.DataFrame` with the following canonical schema:
  - `timestamp`: `datetime64[ns, UTC]` - The timestamp of the sensor reading.
  - `temperature`: `float64` - Temperature in degrees Celsius.
  - `humidity`: `float64` - Relative humidity percentage (0-100).
  - `co2`: `float64` - CO2 concentration in parts per million (PPM).
  - `light_intensity`: `float64` - Light intensity in micromoles per square meter per second (µmol/m²/s).

### Error Handling

- `ConnectionError`: Raised by the `connect` method if the adapter cannot establish a connection with the data source.
- `DataReadError`: Raised by the `read_data` method if the data cannot be fetched from the source after a successful connection.
- `ValidationError`: Raised by the `normalize_data` method if the raw data does not conform to the expected format (e.g., missing columns, incorrect data types).

## 3. Concrete Implementations

### a. CSV Adapter

The `CSVAdapter` is the first implementation, designed to read data from local CSV files. This is primarily for testing, model development, and handling manual data exports.

**Implementation Details:**

- `connect()`: Checks if the specified CSV file path exists.
- `read_data()`: Reads the CSV into a DataFrame using `pd.read_csv()`.
- `normalize_data()`: Renames columns based on a provided mapping and converts data types to match the canonical schema.

**Example Usage:**

```python
# Hypothetical usage
mapping = {
    "DateTime": "timestamp",
    "Temp_C": "temperature",
    "RH_percent": "humidity",
    "CO2_ppm": "co2",
    "Light_umol": "light_intensity"
}
csv_adapter = CSVAdapter(filepath="/path/to/data.csv", column_mapping=mapping)
data = csv_adapter.get_data()
```

### b. Priva Adapter (Stub)

Priva is a common greenhouse control system. An adapter will be built to integrate with their API. This section serves as a placeholder for that future work.

**Implementation Sketch:**

```python
class PrivaAdapter(AbstractVendorAdapter):
    def __init__(self, api_key: str, api_endpoint: str):
        self.api_key = api_key
        self.api_endpoint = api_endpoint
        self.client = None

    def connect(self):
        # Initialize and authenticate API client
        # self.client = PrivaApiClient(api_key=self.api_key)
        # self.client.authenticate()
        pass

    def disconnect(self):
        # self.client.close_session()
        pass

    def read_data(self):
        # Fetch data from self.api_endpoint
        # return response_to_dataframe(self.client.get(...))
        pass

    def normalize_data(self, raw_data: pd.DataFrame) -> pd.DataFrame:
        # Rename and cast columns from the Priva API response
        pass
```

## 4. Data Normalization Layer

The normalization layer is not a separate component but rather a responsibility of each adapter, implemented within the `normalize_data` method. This design choice keeps all vendor-specific logic encapsulated within its own adapter.

**Responsibilities:**

1.  **Column Mapping:** Each adapter will contain a mapping from vendor-specific column names to the canonical names.
2.  **Unit Conversion:** If a vendor provides data in a non-standard unit (e.g., Temperature in Fahrenheit), the adapter must convert it to the canonical unit (Celsius).
3.  **Data Type Casting:** Ensure all columns in the final DataFrame match the data types defined in the canonical schema.
4.  **Timezone Handling:** All timestamps must be converted to UTC.

## 5. Future Extensibility

The adapter pattern makes it straightforward to add new vendors. The process for adding a `NewVendorAdapter` would be:

1.  Create a new class `NewVendorAdapter` that inherits from `AbstractVendorAdapter`.
2.  Implement the `connect`, `disconnect`, `read_data`, and `normalize_data` methods with the logic specific to that vendor's API or data format.
3.  Instantiate `NewVendorAdapter` in the data ingestion service, configured with the necessary credentials or paths.
4.  No changes would be needed in the core application logic, as it only interacts with the `AbstractVendorAdapter` interface.

This design decouples the data acquisition logic from the data processing and modeling logic, ensuring the long-term maintainability of the AI Agronomist platform.

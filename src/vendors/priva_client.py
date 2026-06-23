import pandas as pd
from typing import Any, Dict, List
from datetime import datetime, timezone

from src.vendors.base import (
    DataSourceAdapter,
    DataSourceConnectionError,
    DataReadError,
    NormalizationError,
    DataValidationError
)

class PrivaClient(DataSourceAdapter):
    """
    Concrete implementation of DataSourceAdapter for Priva sensor integration.
    
    This class acts as an integration adapter for the Priva cloud/local platform.
    In this foundation/stub phase, it simulates API connectivity, reads raw proprietary
    telemetry streams, and normalizes them into the canonical schema required by downstream
    machine learning models and relational databases.
    """

    def __init__(self, config: Dict[str, Any] = None):
        """
        Initializes the Priva API client (stub).
        
        Args:
            config: A dictionary containing connection inputs and credentials:
                - api_key/credential_token: Token used for authentication
                - base_url: Priva API host url (defaults to "https://api.priva.com")
                - tenant_id: Tenant identifier
                - greenhouse_id: Greenhouse identifier
        """
        super().__init__(config)
        self.api_key = self.config.get("api_key") or self.config.get("credential_token")
        self.base_url = self.config.get("base_url") or "https://api.priva.com"
        self.connected = False

    def connect(self) -> None:
        """
        Establishes a simulated connection to the Priva API endpoint.
        
        Raises:
            DataSourceConnectionError: If connection configurations are missing
                                       or if the simulated endpoint is unreachable.
        """
        if not self.api_key:
            raise DataSourceConnectionError("Cannot connect to Priva: api_key / credential_token is empty.")
        
        # Simulate connection establishment
        self.connected = True

    def disconnect(self) -> None:
        """
        Gracefully terminates the connection instance and deallocates resources.
        """
        self.connected = False

    def read(self) -> pd.DataFrame:
        """
        Reads raw sensor data directly from the vendor's source channel.
        
        Returns:
            A pandas DataFrame representing raw sensor telemetry using Priva's proprietary format.
            
        Raises:
            DataSourceConnectionError: If called before establishing connection.
            DataReadError: If fetching the telemetry stream fails.
        """
        if not self.connected:
            raise DataSourceConnectionError("API client is not connected. Call connect() first.")
            
        try:
            start_dt = self.config.get("start_time")
            end_dt = self.config.get("end_time")
            
            # Default to the last 24 hours if timestamps are not provided
            if not end_dt:
                end_dt = datetime.now(timezone.utc)
            if not start_dt:
                start_dt = end_dt - pd.Timedelta(hours=24)
                
            # Convert string representations to Timestamp objects
            if isinstance(start_dt, str):
                start_dt = pd.to_datetime(start_dt)
            if isinstance(end_dt, str):
                end_dt = pd.to_datetime(end_dt)
                
            # Ensure timezone awareness for dates generated
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
                
            # Simulate 24 readings within the range
            date_range = pd.date_range(start=start_dt, end=end_dt, periods=24)
            
            # Priva raw format columns:
            # - time (ISO UTC string format)
            # - ambient_temp (temperature in °C)
            # - rel_humidity (relative humidity percent)
            # - co2_level (CO2 level in ppm)
            # - radiation_lux (ambient light intensity in lux)
            raw_data = pd.DataFrame({
                "time": [dt.isoformat() for dt in date_range],
                "ambient_temp": [22.4 + (i % 6) * 0.4 for i in range(len(date_range))],
                "rel_humidity": [58.2 + (i % 8) * 1.1 for i in range(len(date_range))],
                "co2_level": [410.0 + (i % 10) * 12.5 for i in range(len(date_range))],
                "radiation_lux": [750.0 + (i % 5) * 110.0 for i in range(len(date_range))]
            })
            
            return raw_data
        except Exception as e:
            raise DataReadError(f"Failed to read telemetry stream from Priva: {str(e)}") from e

    def normalize(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Normalizes the raw DataFrame structure to standard canonical schema formatting (renaming columns, 
        timezone conversion to UTC, casting data types).
        
        Args:
            data: Raw DataFrame direct from the read stream.
            
        Returns:
            A pandas DataFrame formatted as canonical schema:
            - timestamp: datetime64[ns, UTC]
            - temperature: float64
            - humidity: float64
            - co2: float64
            - light: float64
            
        Raises:
            NormalizationError: If required mapping columns are absent, or type-conversion fails.
        """
        required_raw_cols = ["time", "ambient_temp", "rel_humidity", "co2_level", "radiation_lux"]
        for col in required_raw_cols:
            if col not in data.columns:
                raise NormalizationError(f"Priva client raw column '{col}' is missing.")
                
        try:
            normalized = pd.DataFrame()
            
            # Localize and convert timestamp column to UTC
            timestamps = pd.to_datetime(data["time"], format="ISO8601")
            if timestamps.dt.tz is None:
                timestamps = timestamps.dt.tz_localize("UTC")
            else:
                timestamps = timestamps.dt.tz_convert("UTC")
                
            normalized["timestamp"] = timestamps
            normalized["temperature"] = data["ambient_temp"].astype("float64")
            normalized["humidity"] = data["rel_humidity"].astype("float64")
            normalized["co2"] = data["co2_level"].astype("float64")
            normalized["light"] = data["radiation_lux"].astype("float64")
            
            return normalized
        except Exception as e:
            raise NormalizationError(f"Error transforming Priva data: {str(e)}") from e

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
        try:
            return self.validate_canonical_schema(data)
        except DataValidationError as e:
            raise e
        except Exception as e:
            raise DataValidationError(f"Unexpected validation error occurred: {str(e)}") from e

    @property
    def name(self) -> str:
        """Vendor name (csv, priva, etc.)"""
        return "priva"

    # =============================================================================
    # Methods for Phase 1.5 Scaffolding
    # =============================================================================

    async def fetch_data(self, tenant_id: str, greenhouse_id: str, 
                         start: datetime, end: datetime) -> pd.DataFrame:
        """
        Asynchronously fetches and processes sensor data from the Priva API.
        
        Phase 1: Returns mock data aligned with given timeframe coordinates.
        Phase 1.5: Replaces with actual HTTP client orchestrations.
        """
        self.config["start_time"] = start
        self.config["end_time"] = end
        self.config["tenant_id"] = tenant_id
        self.config["greenhouse_id"] = greenhouse_id
        
        # Run standard lifecycle orchestrator synchronously
        return self.get_data()

    async def _authenticate(self) -> str:
        """Gets OAuth token from Priva (stub)"""
        return "mock_oauth_token_priva_12345"

    async def _list_greenhouses(self) -> List[str]:
        """Lists greenhouses for this customer (stub)"""
        return ["greenhouse_01", "greenhouse_02"]

    async def _get_sensor_data(self, greenhouse_id: str, start: datetime, 
                              end: datetime) -> Dict[str, Any]:
        """Gets sensor readings from Priva API (stub)"""
        return {
            "greenhouse": greenhouse_id,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "sensors": [
                {"id": "temp_01", "type": "temperature", "unit": "C"},
                {"id": "hum_01", "type": "humidity", "unit": "%"},
                {"id": "co2_01", "type": "co2", "unit": "ppm"},
                {"id": "lighting_01", "type": "light", "unit": "lux"}
            ]
        }

import pytest
import pandas as pd
from datetime import datetime, timezone
from src.vendors.base import DataSourceAdapter, DataValidationError, DataSourceConnectionError
from src.vendors.priva_client import PrivaClient

def test_priva_client_init():
    """Verify that PrivaClient initializes with correct configs and defaults."""
    config = {
        "api_key": "test_api_key_123",
        "base_url": "https://api.priva.test",
        "tenant_id": "tenant_abc",
        "greenhouse_id": "greenhouse_x"
    }
    client = PrivaClient(config=config)
    assert client.api_key == "test_api_key_123"
    assert client.base_url == "https://api.priva.test"
    assert client.config == config
    assert client.name == "priva"
    assert issubclass(PrivaClient, DataSourceAdapter)


def test_priva_client_connect_disconnect():
    """Verify state transitions for connect and disconnect."""
    client = PrivaClient(config={"api_key": "some_key"})
    assert client.connected is False
    
    client.connect()
    assert client.connected is True
    
    client.disconnect()
    assert client.connected is False


def test_priva_client_connect_fails_without_key():
    """Verify connect raises error if api_key is missing."""
    client = PrivaClient(config={"api_key": ""})
    with pytest.raises(DataSourceConnectionError, match="api_key / credential_token is empty"):
        client.connect()


def test_priva_client_read_requires_connection():
    """Verify reading fails if the client is not connected."""
    client = PrivaClient(config={"api_key": "some_key"})
    with pytest.raises(DataSourceConnectionError, match="API client is not connected"):
        client.read()


def test_priva_client_read_default_timeframe():
    """Verify reading produces valid mock Priva structure with default 24h timeframe."""
    client = PrivaClient(config={"api_key": "some_key"})
    client.connect()
    
    raw_df = client.read()
    assert isinstance(raw_df, pd.DataFrame)
    assert len(raw_df) == 24
    
    expected_cols = ["time", "ambient_temp", "rel_humidity", "co2_level", "radiation_lux"]
    for col in expected_cols:
        assert col in raw_df.columns


def test_priva_client_normalize_and_validate():
    """Verify normalization maps Priva columns to canonical schema, and passes validation."""
    client = PrivaClient(config={"api_key": "some_key"})
    client.connect()
    
    raw_df = client.read()
    normalized_df = client.normalize(raw_df)
    
    # Assert columns mapping
    canonical_cols = ["timestamp", "temperature", "humidity", "co2", "light"]
    for col in canonical_cols:
        assert col in normalized_df.columns
        
    assert pd.api.types.is_datetime64_any_dtype(normalized_df["timestamp"])
    assert normalized_df["timestamp"].dt.tz is not None # Timezone-aware
    
    # Validate the normalized data passes canonical checks
    assert client.validate(normalized_df) is True


@pytest.mark.async_io
@pytest.mark.asyncio
async def test_priva_client_fetch_data_integration():
    """Verify high level async fetch_data lifecycle orchestration."""
    client = PrivaClient(config={"api_key": "some_key"})
    start = datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc)
    
    normalized_df = await client.fetch_data(
        tenant_id="tenant_1",
        greenhouse_id="gh_2",
        start=start,
        end=end
    )
    
    assert isinstance(normalized_df, pd.DataFrame)
    assert len(normalized_df) == 24
    assert list(normalized_df.columns) == ["timestamp", "temperature", "humidity", "co2", "light"]


@pytest.mark.asyncio
async def test_priva_scaffold_stubs():
    """Verify that all Phase 1.5 scaffold stub endpoints are accessible and return placeholders."""
    client = PrivaClient(config={"api_key": "some_key"})
    
    token = await client._authenticate()
    assert isinstance(token, str)
    assert len(token) > 0
    
    ghs = await client._list_greenhouses()
    assert isinstance(ghs, list)
    assert len(ghs) > 0
    
    sensor_meta = await client._get_sensor_data(
        greenhouse_id="gh_1",
        start=datetime.now(timezone.utc),
        end=datetime.now(timezone.utc)
    )
    assert isinstance(sensor_meta, dict)
    assert "sensors" in sensor_meta

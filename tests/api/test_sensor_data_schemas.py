"""
Unit tests for the Pydantic v2 schemas located in src/api/schemas/sensor_data.py.
"""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError
from src.api.schemas.sensor_data import (
    SensorReadingCreate,
    SensorReadingResponse,
    UploadRequest,
    UploadResponse,
    InferenceRequest,
    InferenceResponse,
)


def test_sensor_reading_create_valid():
    """Tests that a valid SensorReadingCreate parses successfully."""
    data = {
        "timestamp": "2026-06-08T18:32:02Z",
        "temperature": 24.5,
        "humidity": 65.0,
        "co2": 450.0,
        "light": 800.0,
    }
    reading = SensorReadingCreate(**data)
    assert reading.temperature == 24.5
    assert reading.humidity == 65.0
    assert reading.co2 == 450.0
    assert reading.light == 800.0
    assert isinstance(reading.timestamp, datetime)


@pytest.mark.parametrize(
    "field,invalid_value",
    [
        ("temperature", -1.0),
        ("temperature", 51.0),
        ("humidity", -0.1),
        ("humidity", 101.0),
        ("co2", -5.0),
        ("co2", 1005.0),
        ("light", -10.0),
        ("light", 2001.0),
    ],
)
def test_sensor_reading_create_validation_ranges(field, invalid_value):
    """Tests that out-of-range physical bounds raise ValidationErrors."""
    valid_data = {
        "timestamp": "2026-06-08T18:32:02Z",
        "temperature": 24.5,
        "humidity": 65.0,
        "co2": 450.0,
        "light": 800.0,
    }
    valid_data[field] = invalid_value
    with pytest.raises(ValidationError):
        SensorReadingCreate(**valid_data)


def test_sensor_reading_response_valid():
    """Tests parsing a valid SensorReadingResponse (e.g. from ORM attributes)."""
    data = {
        "id": 42,
        "tenant_id": "test_tenant",
        "greenhouse_id": "gh_1",
        "timestamp": datetime.now(timezone.utc),
        "temperature": 25.0,
        "humidity": 60.0,
        "co2": 400.0,
        "light": 500.0,
        "anomaly_score": 0.12,
        "created_at": datetime.now(timezone.utc),
    }
    response = SensorReadingResponse(**data)
    assert response.id == 42
    assert response.tenant_id == "test_tenant"
    assert response.anomaly_score == 0.12


def test_upload_request_response_valid():
    """Tests schemas for data uploads."""
    req_data = {
        "tenant_id": "customer_a",
        "greenhouse_id": "gh_2",
        "readings": [
            {
                "timestamp": "2026-06-08T18:32:02Z",
                "temperature": 24.5,
                "humidity": 65.0,
                "co2": 450.0,
                "light": 800.0,
            }
        ]
    }
    req = UploadRequest(**req_data)
    assert req.tenant_id == "customer_a"
    assert len(req.readings) == 1

    resp_data = {
        "uploaded": 100,
        "errors": 1,
        "skipped": 2,
        "ranges": {
            "temperature": {"min": 20.0, "max": 28.0},
            "humidity": {"min": 50.0, "max": 80.0},
            "co2": {"min": 300.0, "max": 500.0},
            "light": {"min": 100.0, "max": 1000.0}
        },
        "tenant_id": "customer_a",
        "greenhouse_id": "gh_2",
        "message": "Processed successfully"
    }
    resp = UploadResponse(**resp_data)
    assert resp.uploaded == 100
    assert resp.ranges["temperature"].min == 20.0


def test_inference_request_shape_validation():
    """Tests complex dimensional checker inside InferenceRequest."""
    # Build a valid sequence of shape (batch=1, seq_len=20, input_dim=4)
    valid_seq = [[25.0, 60.0, 400.0, 500.0] for _ in range(20)]
    req_data = {
        "tenant_id": "tenant_1",
        "greenhouse_id": "gh_1",
        "sequences": [valid_seq],
    }
    req = InferenceRequest(**req_data)
    assert req.tenant_id == "tenant_1"

    # Test wrong sequence length (e.g. 19 timesteps instead of 20)
    invalid_len_seq = [[25.0, 60.0, 400.0, 500.0] for _ in range(19)]
    invalid_req_data = req_data.copy()
    invalid_req_data["sequences"] = [invalid_len_seq]
    with pytest.raises(ValidationError, match="model architecture requires a exact sequence length of 20"):
        InferenceRequest(**invalid_req_data)

    # Test wrong feature dimension (e.g. 3 features instead of 4)
    invalid_dim_seq = [[25.0, 60.0, 400.0] for _ in range(20)]
    invalid_req_data = req_data.copy()
    invalid_req_data["sequences"] = [invalid_dim_seq]
    with pytest.raises(ValidationError, match="model architecture requires exact feature count/input dimension of 4"):
        InferenceRequest(**invalid_req_data)


def test_inference_response_valid():
    """Tests parsing a valid InferenceResponse."""
    resp_data = {
        "tenant_id": "tenant_1",
        "greenhouse_id": "gh_1",
        "sequences_processed": 5,
        "anomalies_detected": 1,
        "anomaly_scores": [0.05, 0.08, 0.95, 0.12, 0.04],
        "high_anomalies": [
            {
                "sequence_index": 2,
                "score": 0.95,
                "timestamp": "2026-06-08T18:32:02Z",
            }
        ],
    }
    resp = InferenceResponse(**resp_data)
    assert resp.sequences_processed == 5
    assert resp.anomalies_detected == 1
    assert resp.high_anomalies[0].sequence_index == 2

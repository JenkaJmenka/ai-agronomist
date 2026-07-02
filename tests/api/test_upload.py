import pytest_asyncio
"""
Integration and unit tests for the CSV upload API endpoint.

This file covers:
1. Valid CSV ingestion matching the canonical schema.
2. Missing tenant_id validation errors.
3. Invalid CSV format / missing required columns.
4. NaN value range violations.
5. Out of range values for temperature, humidity, CO2, and light.
6. Empty 0-byte CSV file handling.
7. Large file ingestion validation.
8. Data persistence on database commits.
9. Multi-tenant data segregation.
10. Validated response ranges presence verification.
"""

import io
import pytest
import pandas as pd
from fastapi.testclient import TestClient
from fastapi import status
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, func

from src.api.main import app
from src.db.session import get_db
from src.db.models import Base, SensorReading


@pytest.fixture
def sample_csv_data():
    """Generates a valid CSV dataset matching canonical schema rules."""
    columns = "timestamp,temperature,humidity,co2,light\n"
    rows = []
    for i in range(1000):
        # Temperature [0.0, 50.0], Humidity [0.0, 100.0], CO2 [0.0, 1000.0], Light [0.0, 2000.0]
        minutes = i // 60
        seconds = i % 60
        rows.append(f"2026-06-09T11:{minutes:02d}:{seconds:02d}Z,25.5,50.2,401.5,1200.0")
    return columns + "\n".join(rows)


@pytest.fixture
async def test_db():
    """Provides an isolated, transient in-memory SQLite database for each test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    TestingSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with TestingSessionLocal() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def test_client(test_db):
    """Overrides the FastAPI database dependency and returns a client."""
    async def override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_upload_valid_csv(test_client, sample_csv_data):
    """POST /api/upload with valid CSV should return 200 and expected upload stats."""
    csv_bytes = io.BytesIO(sample_csv_data.encode("utf-8"))
    response = test_client.post(
        "/api/upload",
        files={"file": ("test.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_1", "greenhouse_id": "house_alpha"}
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["uploaded"] == 1000
    assert data["errors"] == 0
    assert data["tenant_id"] == "tenant_1"
    assert data["greenhouse_id"] == "house_alpha"


def test_upload_missing_tenant_id(test_client, sample_csv_data):
    """POST /api/upload without tenant_id should return 422 block validation error."""
    csv_bytes = io.BytesIO(sample_csv_data.encode("utf-8"))
    response = test_client.post(
        "/api/upload",
        files={"file": ("test.csv", csv_bytes, "text/csv")},
        data={"greenhouse_id": "house_alpha"}  # tenant_id missing
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "detail" in response.json()


def test_upload_invalid_csv_format(test_client):
    """POST with malformed columns (missing light/co2) should succeed with 200 and save 1 RawReading record."""
    malformed_csv = "timestamp,temperature,humidity\n2026-06-09T11:00:00Z,22.4,60.1"
    csv_bytes = io.BytesIO(malformed_csv.encode("utf-8"))
    response = test_client.post(
        "/api/upload",
        files={"file": ("test.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_1"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["uploaded"] == 1


def test_upload_nan_values(test_client):
    """POST with NaN in any column fields should succeed with 200 and save 1 RawReading record."""
    nan_csv = "timestamp,temperature,humidity,co2,light\n2026-06-09T11:00:00Z,NaN,60.1,400,1000"
    csv_bytes = io.BytesIO(nan_csv.encode("utf-8"))
    response = test_client.post(
        "/api/upload",
        files={"file": ("test.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_1"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["uploaded"] == 1


def test_upload_out_of_range(test_client):
    """POST with temperature out of bounds (55.0 > 50.0) should succeed with 200 and save 1 RawReading record."""
    out_of_bounds_csv = "timestamp,temperature,humidity,co2,light\n2026-06-09T11:00:00Z,55.0,60.1,400,1000"
    csv_bytes = io.BytesIO(out_of_bounds_csv.encode("utf-8"))
    response = test_client.post(
        "/api/upload",
        files={"file": ("test.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_1"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["uploaded"] == 1


def test_upload_empty_file(test_client):
    """POST with a 0-byte CSV file should fail with 400."""
    csv_bytes = io.BytesIO(b"")
    response = test_client.post(
        "/api/upload",
        files={"file": ("test.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_1"}
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "invalid csv format" in str(response.json()).lower()


def test_upload_large_file(test_client):
    """POST 100,000 rows CSV should parse and upload successfully."""
    columns = "timestamp,temperature,humidity,co2,light\n"
    large_csv = columns + "".join(
        f"2026-06-{6 + i//86400:02d}T{(i//3600)%24:02d}:{(i//60)%60:02d}:{i%60:02d}Z,25.0,50.0,400.0,1200.0\n"
        for i in range(100000)
    )
    csv_bytes = io.BytesIO(large_csv.encode("utf-8"))
    
    response = test_client.post(
        "/api/upload",
        files={"file": ("test_large.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_large_scale"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["uploaded"] == 100000


@pytest.mark.asyncio
async def test_upload_data_persists(test_client, test_db, sample_csv_data):
    """Verify that records uploaded successfully are truly committed to the database."""
    csv_bytes = io.BytesIO(sample_csv_data.encode("utf-8"))
    response = test_client.post(
        "/api/upload",
        files={"file": ("test.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_persistent"}
    )
    assert response.status_code == status.HTTP_200_OK

    # Query the test db directly to assert count
    result = await test_db.execute(select(func.count(SensorReading.id)).where(SensorReading.tenant_id == "tenant_persistent"))
    count = result.scalar()
    assert count == 1000


@pytest.mark.asyncio
async def test_upload_multi_tenant_isolation(test_client, test_db, sample_csv_data):
    """Verify that uploads for tenant_1 isolate correctly from tenant_2."""
    # Tenant 1 Upload
    csv_bytes_1 = io.BytesIO(sample_csv_data.encode("utf-8"))
    res1 = test_client.post(
        "/api/upload",
        files={"file": ("test1.csv", csv_bytes_1, "text/csv")},
        data={"tenant_id": "tenant_alpha"}
    )
    assert res1.status_code == status.HTTP_200_OK

    # Tenant 2 Upload
    csv_bytes_2 = io.BytesIO(sample_csv_data.encode("utf-8"))
    res2 = test_client.post(
        "/api/upload",
        files={"file": ("test2.csv", csv_bytes_2, "text/csv")},
        data={"tenant_id": "tenant_beta"}
    )
    assert res2.status_code == status.HTTP_200_OK

    # Check isolation in Database queries
    res_alpha = await test_db.execute(select(func.count(SensorReading.id)).where(SensorReading.tenant_id == "tenant_alpha"))
    assert res_alpha.scalar() == 1000

    res_beta = await test_db.execute(select(func.count(SensorReading.id)).where(SensorReading.tenant_id == "tenant_beta"))
    assert res_beta.scalar() == 1000


def test_upload_response_ranges(test_client):
    """Check that the upload response contains observed min/max metrics for all sensors."""
    # Custom ranges to assert exact min/max mapping
    custom_csv = (
        "timestamp,temperature,humidity,co2,light\n"
        "2026-06-09T11:00:00Z,10.0,30.0,200.0,500.0\n"
        "2026-06-09T11:01:00Z,40.0,80.0,800.0,1500.0\n"
    )
    csv_bytes = io.BytesIO(custom_csv.encode("utf-8"))
    response = test_client.post(
        "/api/upload",
        files={"file": ("test.csv", csv_bytes, "text/csv")},
        data={"tenant_id": "tenant_ranges"}
    )
    assert response.status_code == status.HTTP_200_OK
    ranges = response.json()["ranges"]
    
    # Check ranges schema keys and min/max limits
    assert ranges["temperature"] == {"min": 10.0, "max": 40.0}
    assert ranges["humidity"] == {"min": 30.0, "max": 80.0}
    assert ranges["co2"] == {"min": 200.0, "max": 800.0}
    assert ranges["light"] == {"min": 500.0, "max": 1500.0}

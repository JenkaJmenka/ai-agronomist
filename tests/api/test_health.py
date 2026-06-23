import pytest_asyncio
"""
Unit and integration tests for the FastAPI health check endpoints.

This module verifies the behavior of:
1. The basic `/health` status endpoint (200 OK + "status: ok" payload).
2. The interactive `/health/db` connectivity endpoint under normal and failure conditions.
3. Middlewares (CORS and Custom Request ID logging propagation filters).
4. Authentication bypass (confirming no auth headers are required to probe service status).
"""

import pytest
from fastapi.testclient import TestClient
from fastapi import status
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.exc import SQLAlchemyError

from src.api.main import app
from src.db.session import get_db


@pytest.fixture
def client():
    """Test client fixture that handles startup/shutdown lifespans to verify app initialization."""
    with TestClient(app) as test_client:
        yield test_client


def test_basic_health_endpoint_success(client: TestClient) -> None:
    """
    GIVEN a running FastAPI application
    WHEN a GET request is made to '/health'
    THEN the response status should be 200 OK and the payload should match standard contracts.
    """
    response = client.get("/health")
    assert response.status_code == status.HTTP_200_OK
    
    data = response.json()
    assert data == {"status": "ok"}
    
    # Verify diagnostic and correlation headers are injected by the custom middleware
    assert "X-Request-ID" in response.headers
    assert "X-Process-Time-Ms" in response.headers


def test_basic_health_endpoint_no_authorization_required(client: TestClient) -> None:
    """
    GIVEN a running FastAPI application
    WHEN a GET request is made to '/health' without authorization headers
    THEN the response should bypass auth and return status 200 OK.
    """
    # Simulate completely empty authorization header context
    response = client.get("/health", headers={})
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}


def test_db_health_endpoint_success(client: TestClient) -> None:
    """
    GIVEN the database is responsive and available
    WHEN a GET request is made to '/health/db'
    THEN the response status should be 200 OK and describe a connected database.
    """
    # Create an async database session mock
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 1
    mock_db.execute.return_value = mock_result

    # Override get_db dependency
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        response = client.get("/health/db")
        assert response.status_code == status.HTTP_200_OK
        
        data = response.json()
        assert data == {"status": "ok", "database": "connected"}
        
        # Ensure query SELECT 1 was triggered
        mock_db.execute.assert_called_once()
    finally:
        app.dependency_overrides.clear()


def test_db_health_endpoint_sqlalchemy_exception(client: TestClient) -> None:
    """
    GIVEN a database connectivity problem or a locked table sequence (SQLAlchemyError)
    WHEN a GET request is made to '/health/db'
    THEN the response status should be 503 SERVICE UNAVAILABLE and contain "database: disconnected".
    """
    mock_db = AsyncMock()
    mock_db.execute.side_effect = SQLAlchemyError("SQLITE_BUSY: Database file is locked.")

    # Override get_db dependency
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        response = client.get("/health/db")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        
        data = response.json()
        assert data == {"status": "error", "database": "disconnected"}
    finally:
        app.dependency_overrides.clear()


def test_db_health_endpoint_generic_exception(client: TestClient) -> None:
    """
    GIVEN a severe system error or unresolved database session query failure (Exception)
    WHEN a GET request is made to '/health/db'
    THEN the response status should be 503 SERVICE UNAVAILABLE and return the expected payload.
    """
    mock_db = AsyncMock()
    mock_db.execute.side_effect = Exception("Unknown severe database/networking connection timeout error.")

    # Override get_db dependency
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        response = client.get("/health/db")
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        
        data = response.json()
        assert data == {"status": "error", "database": "disconnected"}
    finally:
        app.dependency_overrides.clear()


def test_health_cors_headers_handling(client: TestClient) -> None:
    """
    GIVEN a third-party application calling the endpoint from an allowed localhost origin (e.g., Streamlit on 8501)
    WHEN a GET request is made to '/health' with Origin headers
    THEN the response should return Access-Control-Allow-Origin headers matching the request.
    """
    headers = {"Origin": "http://localhost:8501"}
    response = client.get("/health", headers=headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.headers.get("Access-Control-Allow-Origin") == "http://localhost:8501"


def test_health_custom_request_id_propagation(client: TestClient) -> None:
    """
    GIVEN a trace log execution stream needing correlation tracking
    WHEN a client sends a specific 'X-Request-ID' header
    THEN the response should preserve and propagate the same tracking ID value in its headers.
    """
    test_trace_id = "test-corr-id-9876543210"
    response = client.get("/health", headers={"X-Request-ID": test_trace_id})
    assert response.status_code == status.HTTP_200_OK
    assert response.headers.get("X-Request-ID") == test_trace_id


# =============================================================================
# Deliverables Name Compliance Wrappers
# =============================================================================

def test_health_endpoint_returns_ok(client: TestClient) -> None:
    """GET /health -> status 200, body {"status": "ok"}"""
    test_basic_health_endpoint_success(client)


def test_health_db_endpoint_connection_ok(client: TestClient) -> None:
    """GET /health/db -> status 200, {"status": "ok", "database": "connected"}"""
    test_db_health_endpoint_success(client)


def test_health_db_endpoint_connection_fails(client: TestClient) -> None:
    """GET /health/db (DB failure) -> status 503, {"status": "error", "database": "disconnected"}"""
    test_db_health_endpoint_sqlalchemy_exception(client)


def test_health_endpoint_no_auth_needed(client: TestClient) -> None:
    """No Authorization header -> still returns 200"""
    test_basic_health_endpoint_no_authorization_required(client)


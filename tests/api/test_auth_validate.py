"""Route tests for GET /api/auth/validate (G7 key validation for the BFF)."""

from __future__ import annotations

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from src.api.auth import clear_test_api_keys, register_test_api_key
from src.api.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.routes.auth.AUTH_ENABLED", True)
    register_test_api_key("tenant_a", "secret-a")
    yield
    clear_test_api_keys()


def test_validate_with_auth_disabled_is_ok_without_scope(client, monkeypatch):
    monkeypatch.setattr("src.api.routes.auth.AUTH_ENABLED", False)
    response = client.get("/api/auth/validate")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"ok": True, "tenant_scope": None}


def test_validate_valid_key_returns_tenant_scope(client, auth_enabled):
    response = client.get(
        "/api/auth/validate", headers={"X-API-Key": "secret-a"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"ok": True, "tenant_scope": "tenant_a"}


def test_validate_unknown_key_is_not_ok_but_still_200(client, auth_enabled):
    response = client.get(
        "/api/auth/validate", headers={"X-API-Key": "wrong-key"}
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"ok": False, "tenant_scope": None}


def test_validate_missing_key_is_not_ok(client, auth_enabled):
    response = client.get("/api/auth/validate")
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"ok": False, "tenant_scope": None}

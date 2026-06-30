"""Tests for dashboard HTTP helpers."""

from src.dashboard.services.http_utils import build_api_headers


def test_build_api_headers_includes_api_key():
    headers = build_api_headers("secret-key")
    assert headers == {"X-API-Key": "secret-key"}


def test_build_api_headers_empty_when_no_key():
    headers = build_api_headers(None)
    assert headers == {}

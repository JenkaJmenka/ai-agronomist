"""Stable fastapi-cache key builder — ignores Depends(db) identity."""

from __future__ import annotations

from starlette.requests import Request

from src.api.cache_keys import request_scoped_key_builder


def _fake_endpoint() -> None:
    return None


class _FakeSession:
    def __init__(self, addr: int) -> None:
        self._addr = addr

    def __repr__(self) -> str:
        return f"<AsyncSession at 0x{self._addr:x}>"


def test_same_query_different_db_sessions_share_key(monkeypatch) -> None:
    from fastapi_cache import FastAPICache

    monkeypatch.setattr(FastAPICache, "get_prefix", classmethod(lambda cls: "fastapi-cache"))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/anomalies/enriched",
        "raw_path": b"/api/anomalies/enriched",
        "query_string": b"tenant_id=t1&threshold=0.15&limit=500",
        "headers": [],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    request = Request(scope)

    key_a = request_scoped_key_builder(
        _fake_endpoint,
        request=request,
        kwargs={"tenant_id": "t1", "db": _FakeSession(0x1111)},
    )
    key_b = request_scoped_key_builder(
        _fake_endpoint,
        request=request,
        kwargs={"tenant_id": "t1", "db": _FakeSession(0x2222)},
    )
    assert key_a == key_b
    assert key_a.startswith("fastapi-cache::")


def test_kwargs_fallback_drops_db(monkeypatch) -> None:
    from fastapi_cache import FastAPICache

    monkeypatch.setattr(FastAPICache, "get_prefix", classmethod(lambda cls: "fastapi-cache"))

    key_a = request_scoped_key_builder(
        _fake_endpoint,
        kwargs={"tenant_id": "t1", "threshold": 0.15, "db": _FakeSession(0x1111)},
    )
    key_b = request_scoped_key_builder(
        _fake_endpoint,
        kwargs={"tenant_id": "t1", "threshold": 0.15, "db": _FakeSession(0x9999)},
    )
    assert key_a == key_b

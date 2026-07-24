"""Stable fastapi-cache key builder — ignores Depends(db) identity."""

from __future__ import annotations

from starlette.requests import Request

from src.api.cache_keys import _safe_args_repr, request_scoped_key_builder


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


# ---------------------------------------------------------------------------
# _safe_args_repr — MINOR #12: no-request fallback must not embed unstable
# object reprs (memory addresses) in the cache key.
# ---------------------------------------------------------------------------


class TestSafeArgsRepr:
    """_safe_args_repr replaces non-primitive args with their type name."""

    def test_empty_args_returns_empty_tuple_repr(self) -> None:
        assert _safe_args_repr(None) == "()"
        assert _safe_args_repr(()) == "()"

    def test_primitives_are_included_verbatim(self) -> None:
        result = _safe_args_repr(("hello", 42, 3.14, True, None))
        assert "'hello'" in result
        assert "42" in result
        assert "3.14" in result
        assert "True" in result
        assert "None" in result

    def test_non_primitive_replaced_by_type_name(self) -> None:
        session = _FakeSession(0xDEAD)
        result = _safe_args_repr((session,))
        # Must NOT contain the memory-address repr.
        assert "0xdead" not in result.lower()
        assert "<AsyncSession" not in result
        # Must contain the class name as a stable stand-in.
        assert "_FakeSession" in result

    def test_mixed_args_stable_across_object_identities(self) -> None:
        """Same primitives + different object identity → same repr."""
        result_a = _safe_args_repr(("t1", _FakeSession(0x1111)))
        result_b = _safe_args_repr(("t1", _FakeSession(0x9999)))
        assert result_a == result_b

    def test_single_arg_wrapped_correctly(self) -> None:
        """Single-element tuple must have trailing comma to be valid."""
        result = _safe_args_repr(("only",))
        assert result.startswith("(")
        assert result.endswith(",)")

    def test_false_and_zero_are_primitive(self) -> None:
        """False and 0 must not be treated as falsy non-primitives."""
        result = _safe_args_repr((False, 0, ""))
        assert "False" in result
        assert "0" in result
        assert "''" in result


class TestFallbackKeyStabilityWithNonPrimitiveArgs:
    """No-request fallback key must not vary when a non-primitive arg changes identity."""

    def test_non_primitive_positional_arg_gives_stable_key(self, monkeypatch) -> None:
        from fastapi_cache import FastAPICache

        monkeypatch.setattr(FastAPICache, "get_prefix", classmethod(lambda cls: "fastapi-cache"))

        key_a = request_scoped_key_builder(
            _fake_endpoint,
            args=(_FakeSession(0xAAAA),),
        )
        key_b = request_scoped_key_builder(
            _fake_endpoint,
            args=(_FakeSession(0xBBBB),),
        )
        assert key_a == key_b, (
            "Keys must be identical when only object identity (address) changes"
        )

    def test_primitive_positional_arg_varies_key(self, monkeypatch) -> None:
        """Different primitive values MUST produce different keys."""
        from fastapi_cache import FastAPICache

        monkeypatch.setattr(FastAPICache, "get_prefix", classmethod(lambda cls: "fastapi-cache"))

        key_a = request_scoped_key_builder(_fake_endpoint, args=("tenant_a",))
        key_b = request_scoped_key_builder(_fake_endpoint, args=("tenant_b",))
        assert key_a != key_b, "Different primitive arg values must yield different cache keys"

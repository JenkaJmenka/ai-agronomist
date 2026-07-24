"""Stable fastapi-cache key builders.

The library default hashes ``args`` + ``kwargs``, which includes
``db: AsyncSession = Depends(...)``. Session object identity changes every
request, so every call misses Redis and writes a duplicate key.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Optional

from starlette.requests import Request
from starlette.responses import Response

# Injected FastAPI dependencies that must never enter the cache key.
_UNSTABLE_KWARGS = frozenset({"db", "request", "response", "background_tasks"})

# Python primitive types whose repr() is stable across processes and object
# lifetimes.  Anything else (e.g. an AsyncSession or a Pydantic model) would
# embed the object's memory address in the repr, producing a different key on
# every call.
_PRIMITIVE_TYPES = (str, int, float, bool, type(None))


def _stable_kwargs(kwargs: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not kwargs:
        return {}
    return {key: value for key, value in kwargs.items() if key not in _UNSTABLE_KWARGS}


def _safe_args_repr(args: Optional[tuple[Any, ...]]) -> str:
    """Return a stable string representation of positional args.

    Only primitive values (str, int, float, bool, None) are included
    verbatim.  Any other positional arg is replaced by its type name so
    that unstable ``object.__repr__()`` output (e.g. memory addresses from
    AsyncSession or Pydantic model instances) cannot vary the cache key.
    """
    if not args:
        return "()"
    parts = [
        repr(a) if isinstance(a, _PRIMITIVE_TYPES) else type(a).__name__
        for a in args
    ]
    return f"({', '.join(parts)},)" if len(parts) == 1 else f"({', '.join(parts)})"


def request_scoped_key_builder(
    func: Callable[..., Any],
    namespace: str = "",
    *,
    request: Optional[Request] = None,
    response: Optional[Response] = None,
    args: Optional[tuple[Any, ...]] = None,
    kwargs: Optional[dict[str, Any]] = None,
) -> str:
    """Cache key from route identity + query string (or stable kwargs).

    Prefer the request URL so identical GETs share a key regardless of
    Depends()-injected sessions. Fall back to filtered kwargs when no
    request is present (unit tests / non-HTTP callers).

    In the no-request fallback, positional *args* are sanitised via
    :func:`_safe_args_repr` so that objects with unstable ``repr()`` output
    (e.g. AsyncSession instances whose address changes per call) cannot
    produce colliding or ever-changing cache keys.
    """
    from fastapi_cache import FastAPICache

    prefix = f"{FastAPICache.get_prefix()}:{namespace}:"
    if request is not None:
        payload = (
            f"{func.__module__}:{func.__name__}:"
            f"{request.method}:{request.url.path}?{request.url.query}"
        )
    else:
        payload = (
            f"{func.__module__}:{func.__name__}:"
            f"{_safe_args_repr(args)}:{_stable_kwargs(kwargs)}"
        )
    return prefix + hashlib.md5(payload.encode()).hexdigest()  # nosec:B303

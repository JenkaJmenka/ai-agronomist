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


def _stable_kwargs(kwargs: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not kwargs:
        return {}
    return {key: value for key, value in kwargs.items() if key not in _UNSTABLE_KWARGS}


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
            f"{args or ()}:{_stable_kwargs(kwargs)}"
        )
    return prefix + hashlib.md5(payload.encode()).hexdigest()  # nosec:B303

"""Singleton LiveHub provider for the SSE live-monitoring route.

The hub is constructed lazily on first access so that importing this module
never opens a Redis connection (import-safe when Redis is down).  Each
call to ``subscribe`` creates a fresh per-subscriber connection via the
factory, matching the multi-worker deployment model.

Usage as a FastAPI dependency::

    from src.api.services.live_hub_provider import get_live_hub

    @router.get("/events")
    async def events(hub: LiveHub = Depends(get_live_hub)) -> ...:
        ...
"""

from __future__ import annotations

import os
from typing import Optional

import redis.asyncio as aioredis

from src.live.hub import LiveHub

# Default matches the FastAPICache initialization in src/api/main.py.
_REDIS_URL_DEFAULT = "redis://localhost:6379/0"

_hub: Optional[LiveHub] = None


def _make_redis_client() -> aioredis.Redis:
    """Create a new async Redis client from the REDIS_URL environment variable.

    Called once per :meth:`LiveHub.subscribe` invocation so each SSE connection
    gets its own connection (no sharing across subscribers).
    """
    redis_url = os.environ.get("REDIS_URL", _REDIS_URL_DEFAULT)
    return aioredis.from_url(redis_url, encoding="utf8", decode_responses=False)


def get_live_hub() -> LiveHub:
    """FastAPI dependency: return the module-level LiveHub singleton.

    The hub is constructed on first call; subsequent calls return the cached
    instance.  Construction does *not* open a Redis connection — that happens
    inside :meth:`LiveHub.subscribe` via the factory.

    Override in tests via ``app.dependency_overrides[get_live_hub] = lambda: hub``.
    """
    global _hub
    if _hub is None:
        _hub = LiveHub(redis_factory=_make_redis_client)
    return _hub

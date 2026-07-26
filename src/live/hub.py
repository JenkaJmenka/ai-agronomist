"""LiveHub — Redis Streams fan-out for live monitoring SSE.

Fan-out is worker-safe: events travel through Redis Streams, not process
memory, so multiple readers on different uvicorn workers all see every event.

**Scope suppression is not worker-safe.** ``src.live.scope_control`` stores
the suppression set in process-local memory; suppress/resume calls do not
propagate across workers.  Single-worker deployment is therefore a documented
invariant of this application — see ``src/live/scope_control.py`` for details.

Each subscriber gets its own ``XREAD`` loop; replay-gap detection yields a
synthetic ``reset`` event so the SSE route can inform the client.

This module has **no** database access.  Redis connection errors propagate
as-is; runtime supervision (reconnect, health check) is a later phase.
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Callable, Optional, Tuple, Union

import redis.asyncio as aioredis
from pydantic import BaseModel

from src.config import LIVE_HUB_BLOCK_MS, LIVE_REPLAY_BUFFER_SIZE


async def _safe_aclose(client: aioredis.Redis) -> None:
    """Close an async Redis client, compatible with redis-py 4.x and 5.x.

    redis-py 5.0.1 introduced ``aclose()``; the 4.x series only has
    ``close()``.  This helper prefers ``aclose()`` when present and falls
    back to ``close()`` so the code works across the declared dependency
    range (``redis>=4.2.0,<5.0.0``).
    """
    close_fn = getattr(client, "aclose", None) or getattr(client, "close")
    await close_fn()


def stream_key(tenant_id: str, greenhouse_id: str) -> str:
    """Build the Redis stream key for a tenant/greenhouse pair."""
    return f"live:{tenant_id}:{greenhouse_id}"


class LiveHub:
    """Publish and subscribe to live monitoring events via Redis Streams.

    Constructor accepts **either** a ready ``redis.asyncio.Redis`` client
    (shared across publishers and subscribers) **or** a zero-arg synchronous
    factory that returns a new async client per subscriber (avoids connection
    sharing in multi-subscriber scenarios — useful with ``fakeredis`` in tests).

    Parameters
    ----------
    redis_client:
        An existing async Redis client.  Mutually exclusive with *redis_factory*.
    redis_factory:
        A **synchronous** zero-arg callable that returns a fresh
        ``redis.asyncio.Redis`` client (the returned client is async; the
        factory itself is called without ``await``).  Each :meth:`subscribe`
        invocation calls the factory once so each subscriber gets its own
        connection.
    block_ms:
        ``XREAD`` block timeout in milliseconds.  Defaults to the
        ``LIVE_HUB_BLOCK_MS`` config value.

    Notes
    -----
    The ``subscribe`` generator is cancellation-safe: when the caller's
    ``async for`` loop is broken (e.g. SSE disconnect), the generator's
    ``finally`` clause closes per-subscriber connections created via a
    factory.

    Idle ticks (block timeout with no new entries) yield a synthetic
    ``("", "idle", "{}")`` tuple so the SSE route can emit a keep-alive
    comment — callers should check ``event_type == "idle"`` and handle
    accordingly.
    """

    def __init__(
        self,
        *,
        redis_client: Optional[aioredis.Redis] = None,
        redis_factory: Optional[Callable[[], aioredis.Redis]] = None,
        block_ms: int = LIVE_HUB_BLOCK_MS,
    ) -> None:
        if redis_client is None and redis_factory is None:
            raise ValueError("Provide redis_client or redis_factory")
        self._client = redis_client
        self._factory = redis_factory
        self._block_ms = block_ms
        # Memoized publisher client (created from factory on first publish).
        self._pub_client: Optional[aioredis.Redis] = None

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(
        self,
        tenant_id: str,
        greenhouse_id: str,
        event_type: str,
        payload: BaseModel,
    ) -> str:
        """Append an event to the tenant/greenhouse stream via ``XADD``.

        The stream is capped at ``LIVE_REPLAY_BUFFER_SIZE`` entries
        (approximate trimming).

        Returns the Redis entry ID (used as the SSE ``id:`` field).
        """
        client = await self._get_client()
        key = stream_key(tenant_id, greenhouse_id)
        fields = {
            "type": event_type,
            "data": payload.model_dump_json(),
        }
        entry_id: bytes = await client.xadd(
            key,
            fields,
            maxlen=LIVE_REPLAY_BUFFER_SIZE,
            approximate=True,
        )
        return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)

    async def clear_stream(self, tenant_id: str, greenhouse_id: str) -> bool:
        """Delete the Redis stream for a tenant/greenhouse. Returns True if a key was removed."""
        client = await self._get_client()
        deleted = await client.delete(stream_key(tenant_id, greenhouse_id))
        return bool(deleted)

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        tenant_id: str,
        greenhouse_id: str,
        last_event_id: Optional[str] = None,
    ) -> AsyncIterator[Tuple[str, str, str]]:
        """Yield ``(entry_id, event_type, data_json)`` tuples.

        Replay & gap detection
        ~~~~~~~~~~~~~~~~~~~~~~
        If *last_event_id* is given and still present in the stream,
        everything **after** it is replayed before live-tailing.  If the
        ID has been trimmed, a synthetic ``("", "reset", ...)`` tuple is
        yielded first and the live tail starts from ``$`` (newest).

        Idle heartbeats
        ~~~~~~~~~~~~~~~
        When ``XREAD`` times out (no new entries within ``block_ms``),
        a synthetic ``("", "idle", "{}")`` tuple is yielded so the
        calling SSE route can emit a ``: keep-alive`` comment.

        Cancellation safety
        ~~~~~~~~~~~~~~~~~~~
        Breaking the ``async for`` loop triggers the generator's
        ``finally`` clause, which closes factory-created connections.
        """
        key = stream_key(tenant_id, greenhouse_id)
        client, is_owned = await self._subscriber_client()

        try:
            start_id = "$"

            if last_event_id is not None:
                # Check whether the requested ID is still in the stream.
                is_present = await self._id_in_stream(client, key, last_event_id)
                if is_present:
                    # Replay everything after last_event_id.
                    replay_entries = await client.xrange(
                        key, min=_exclusive_id(last_event_id), max="+",
                    )
                    for raw_id, raw_fields in replay_entries:
                        yield _parse_entry(raw_id, raw_fields)
                    # Continue live-tail from the last replayed entry or
                    # from last_event_id if no replay entries existed.
                    if replay_entries:
                        last_raw = replay_entries[-1][0]
                        start_id = last_raw.decode() if isinstance(last_raw, bytes) else str(last_raw)
                    else:
                        start_id = last_event_id
                else:
                    yield ("", "reset", json.dumps({"reason": "replay-gap"}))

            # Live tail loop.
            while True:
                result = await client.xread(
                    {key: start_id}, block=self._block_ms, count=100,
                )
                if not result:
                    # Block timeout — no new entries.
                    yield ("", "idle", "{}")
                    continue

                for _stream_name, entries in result:
                    for raw_id, raw_fields in entries:
                        parsed = _parse_entry(raw_id, raw_fields)
                        start_id = parsed[0]
                        yield parsed
        finally:
            if is_owned:
                await _safe_aclose(client)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _get_client(self) -> aioredis.Redis:
        """Return the shared client, or a memoized factory-created publisher client."""
        if self._client is not None:
            return self._client
        assert self._factory is not None
        if self._pub_client is None:
            self._pub_client = self._factory()
        return self._pub_client

    async def aclose(self) -> None:
        """Close the memoized publisher client (only if created via factory).

        Per-subscriber clients are closed in their own ``finally`` blocks;
        this method handles only the publisher-side resource.  If a
        ``redis_client`` was passed directly, we do NOT close it — the
        caller owns its lifecycle.
        """
        if self._pub_client is not None:
            await _safe_aclose(self._pub_client)
            self._pub_client = None

    async def _subscriber_client(self) -> Tuple[aioredis.Redis, bool]:
        """Return ``(client, is_owned)`` for a subscriber.

        Factory-created clients are ``is_owned=True`` — the subscriber's
        ``finally`` block must close them.
        """
        if self._factory is not None:
            return self._factory(), True
        assert self._client is not None
        return self._client, False

    @staticmethod
    async def _id_in_stream(
        client: aioredis.Redis,
        key: str,
        entry_id: str,
    ) -> bool:
        """Return ``True`` if *entry_id* is present in the stream."""
        entries = await client.xrange(key, min=entry_id, max=entry_id, count=1)
        return len(entries) > 0


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _exclusive_id(entry_id: str) -> str:
    """Return an exclusive lower bound for XRANGE (``(id`` syntax).

    Redis XRANGE supports ``(`` prefix for exclusive ranges.
    """
    return f"({entry_id}"


def _parse_entry(
    raw_id: Union[bytes, str],
    raw_fields: dict,
) -> Tuple[str, str, str]:
    """Decode a raw Redis stream entry into ``(entry_id, event_type, data_json)``."""
    entry_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
    event_type_raw = raw_fields.get(b"type", raw_fields.get("type", b"unknown"))
    data_raw = raw_fields.get(b"data", raw_fields.get("data", b"{}"))
    event_type = event_type_raw.decode() if isinstance(event_type_raw, bytes) else str(event_type_raw)
    data_json = data_raw.decode() if isinstance(data_raw, bytes) else str(data_raw)
    return (entry_id, event_type, data_json)

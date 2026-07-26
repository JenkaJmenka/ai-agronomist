"""Smoke tests for GET /api/live/events (Phase 2B SSE endpoint).

Strategy
--------
The live SSE generator runs indefinitely by design (LIVE_MAX_STREAM_SECONDS=3600).
To make tests produce a finite response body we monkeypatch the route-level
constant ``src.api.routes.live.LIVE_MAX_STREAM_SECONDS`` to a tiny value
(0.05 s).  fakeredis XREAD with block_ms=10 returns in ~10 ms; the stream
closes naturally after a few idle ticks, and ``httpx.AsyncClient.get()`` returns
the complete buffered body.

Coverage
--------
- Published events arrive as named SSE frames with ``id:``.
- Heartbeat event on first idle tick (LIVE_HEARTBEAT_SECONDS also patched to 0
  so every idle tick becomes a heartbeat, regardless of system-clock values).
- Keep-alive SSE comment when heartbeat interval has not elapsed.
- Last-Event-ID replay (header) delivers missed events before live tail.
- Missing API key → 401 when AUTH_ENABLED.
- Wrong tenant → 403 when AUTH_ENABLED.
"""

from __future__ import annotations

import json
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from src.api.auth import clear_test_api_keys, register_test_api_key
from src.api.main import app
from src.api.services.live_hub_provider import get_live_hub
from src.live.hub import LiveHub

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT = "tenant_1"
_GREENHOUSE = "gh_A"
_API_KEY = "test-live-key"

# fakeredis XREAD with block_ms=10 returns in ~10 ms — keeps tests fast.
_BLOCK_MS = 10

# Tiny stream duration so the generator closes quickly and httpx.get() returns.
_MAX_STREAM_S = 0.05

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    """Shared fakeredis client for a single test."""
    client = FakeRedis()
    yield client
    # Version-safe close: redis-py 4.x has close(), 5.x added aclose().
    close_fn = getattr(client, "aclose", None) or client.close
    await close_fn()


@pytest_asyncio.fixture
async def hub(fake_redis: FakeRedis) -> LiveHub:
    """LiveHub backed by fakeredis with a short block timeout."""
    return LiveHub(redis_client=fake_redis, block_ms=_BLOCK_MS)


@pytest_asyncio.fixture
async def async_client(hub: LiveHub) -> AsyncGenerator[AsyncClient, None]:
    """httpx AsyncClient wired to the FastAPI app with hub overridden."""
    app.dependency_overrides[get_live_hub] = lambda: hub
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.pop(get_live_hub, None)


@pytest.fixture(autouse=True)
def _reset_auth_keys() -> None:
    """Ensure TENANT_API_KEYS is clean before and after each test."""
    clear_test_api_keys()
    yield
    clear_test_api_keys()


@pytest.fixture(autouse=True)
def _patch_stream_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin LIVE_MAX_STREAM_SECONDS to a tiny value so SSE streams terminate.

    Also pins LIVE_HEARTBEAT_SECONDS to 0 so the first idle tick always
    yields a heartbeat frame (avoids relying on wall-clock timing).
    """
    monkeypatch.setattr("src.api.routes.live.LIVE_MAX_STREAM_SECONDS", _MAX_STREAM_S)
    monkeypatch.setattr("src.api.routes.live.LIVE_HEARTBEAT_SECONDS", 0.0)


# ---------------------------------------------------------------------------
# SSE frame parsing helpers
# ---------------------------------------------------------------------------


def _parse_frames(body: str) -> list[dict[str, str]]:
    """Parse an SSE response body into a list of frame dicts.

    Each frame dict may contain: ``id``, ``event``, ``data``, ``comment``.
    """
    frames: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            if current:
                frames.append(current)
                current = {}
            continue
        if line.startswith(": "):
            current["comment"] = line[2:]
        elif ":" in line:
            field, _, value = line.partition(":")
            current[field.strip()] = value.lstrip(" ")
        else:
            current[line.strip()] = ""
    if current:
        frames.append(current)
    return frames


def _make_reading_event() -> "LiveReadingEvent":  # type: ignore[name-defined]
    from datetime import datetime, timezone

    from src.live.events import LiveReadingEvent

    return LiveReadingEvent(
        seq="",
        timestamp=datetime.now(timezone.utc),
        tenant_id=_TENANT,
        greenhouse_id=_GREENHOUSE,
        channels={"temperature": 22.5, "humidity": 65.0},
        sources={"temperature": "src", "humidity": "src"},
    )


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_published_event_arrives_as_named_sse_frame(
    async_client: AsyncClient,
    hub: LiveHub,
) -> None:
    """A published event is delivered with id:, event:, and data: fields."""
    event = _make_reading_event()
    anchor_id = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)
    target_id = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)

    resp = await async_client.get(
        "/api/live/events",
        params={
            "tenant_id": _TENANT,
            "greenhouse_id": _GREENHOUSE,
            "last_event_id": anchor_id,
        },
        timeout=10.0,
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-accel-buffering"] == "no"

    frames = _parse_frames(resp.text)
    reading_frames = [f for f in frames if f.get("id") == target_id]
    assert reading_frames, (
        f"Expected frame with id={target_id!r}; got frames: {frames}"
    )
    rf = reading_frames[0]
    assert rf.get("event") == "reading"
    assert "temperature" in rf.get("data", "")


@pytest.mark.asyncio
async def test_idle_tick_emits_heartbeat(
    async_client: AsyncClient,
) -> None:
    """The first idle tick (no new stream entries) emits a heartbeat event.

    LIVE_HEARTBEAT_SECONDS is patched to 0 so every idle tick is a heartbeat;
    LIVE_MAX_STREAM_SECONDS is patched to 0.05 s so the stream closes quickly.
    """
    resp = await async_client.get(
        "/api/live/events",
        params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        timeout=10.0,
    )
    assert resp.status_code == 200
    frames = _parse_frames(resp.text)
    heartbeat_frames = [f for f in frames if f.get("event") == "heartbeat"]
    assert heartbeat_frames, f"Expected heartbeat frame; got frames: {frames}"
    # Heartbeat data must be valid JSON with a 'ts' key.
    hb_data = json.loads(heartbeat_frames[0]["data"])
    assert "ts" in hb_data


@pytest.mark.asyncio
async def test_keep_alive_comment_when_heartbeat_not_due(
    async_client: AsyncClient,
    hub: LiveHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keep-alive SSE comment is emitted when heartbeat interval has not elapsed.

    We patch LIVE_HEARTBEAT_SECONDS to a very large value so that after the
    first heartbeat, subsequent idle ticks produce keep-alive comments instead.
    """
    # Override to large interval: first idle → heartbeat, rest → keep-alive.
    monkeypatch.setattr("src.api.routes.live.LIVE_HEARTBEAT_SECONDS", 9999.0)
    # Allow multiple idle ticks to fire before the deadline.
    monkeypatch.setattr("src.api.routes.live.LIVE_MAX_STREAM_SECONDS", 0.06)

    resp = await async_client.get(
        "/api/live/events",
        params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        timeout=10.0,
    )
    assert resp.status_code == 200
    # At least one keep-alive SSE comment must appear.
    assert ": keep-alive" in resp.text, (
        f"Expected keep-alive comment; body: {resp.text!r}"
    )


@pytest.mark.asyncio
async def test_last_event_id_header_replays_missed_events(
    async_client: AsyncClient,
    hub: LiveHub,
) -> None:
    """Last-Event-ID header triggers replay of missed events."""
    event = _make_reading_event()
    anchor_id = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)
    missed_id = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)

    resp = await async_client.get(
        "/api/live/events",
        params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        headers={"Last-Event-ID": anchor_id},
        timeout=10.0,
    )
    assert resp.status_code == 200
    assert missed_id in resp.text, (
        f"Missed event {missed_id!r} not replayed in body: {resp.text!r}"
    )


# ---------------------------------------------------------------------------
# Tests — auth rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_api_key_returns_401_when_auth_enabled(
    hub: LiveHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When auth is on, a request without X-API-Key must be rejected with 401."""
    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
    register_test_api_key(_TENANT, _API_KEY)

    app.dependency_overrides[get_live_hub] = lambda: hub
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/live/events",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
            timeout=5.0,
        )
    app.dependency_overrides.pop(get_live_hub, None)

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_wrong_tenant_returns_403_when_auth_enabled(
    hub: LiveHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When auth is on, an API key belonging to a different tenant returns 403."""
    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
    register_test_api_key("other_tenant", "other-key")

    app.dependency_overrides[get_live_hub] = lambda: hub
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/live/events",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
            headers={"X-API-Key": "other-key"},
            timeout=5.0,
        )
    app.dependency_overrides.pop(get_live_hub, None)

    assert resp.status_code == 403

"""Comprehensive tests for GET /api/live/events and /api/live/status.

Covers: SSE frame format exactness (id/event/data ordering); heartbeat
rate-limit; reset frame; Last-Event-ID header AND query param; max-stream-seconds
deadline closes stream; auth 401/403 incl. cross-tenant rejection;
/api/live/status with runtime present, absent, unknown scope.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from src.api.auth import clear_test_api_keys, register_test_api_key
from src.api.main import app
from src.api.services.live_hub_provider import get_live_hub
from src.live.events import LiveReadingEvent
from src.live.hub import LiveHub


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT = "tenant_1"
_GREENHOUSE = "gh_A"
_API_KEY = "test-live-key"
_BLOCK_MS = 10
_MAX_STREAM_S = 0.05


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    client = FakeRedis()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def hub(fake_redis: FakeRedis) -> LiveHub:
    return LiveHub(redis_client=fake_redis, block_ms=_BLOCK_MS)


@pytest_asyncio.fixture
async def async_client(hub: LiveHub) -> AsyncGenerator[AsyncClient, None]:
    app.dependency_overrides[get_live_hub] = lambda: hub
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.pop(get_live_hub, None)


@pytest.fixture(autouse=True)
def _reset_auth_keys() -> None:
    clear_test_api_keys()
    yield
    clear_test_api_keys()


@pytest.fixture(autouse=True)
def _patch_stream_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.api.routes.live.LIVE_MAX_STREAM_SECONDS", _MAX_STREAM_S)
    monkeypatch.setattr("src.api.routes.live.LIVE_HEARTBEAT_SECONDS", 0.0)


# ---------------------------------------------------------------------------
# Frame parsing helper
# ---------------------------------------------------------------------------


def _parse_frames(body: str) -> list[dict[str, str]]:
    """Parse SSE body into frames (list of dicts with id/event/data/comment)."""
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


def _make_reading() -> LiveReadingEvent:
    return LiveReadingEvent(
        seq="",
        timestamp=datetime.now(timezone.utc),
        tenant_id=_TENANT,
        greenhouse_id=_GREENHOUSE,
        channels={"temperature": 22.5, "humidity": 65.0},
        sources={"temperature": "src", "humidity": "src"},
    )


# ---------------------------------------------------------------------------
# SSE frame format exactness
# ---------------------------------------------------------------------------


class TestSSEFrameFormat:

    @pytest.mark.asyncio
    async def test_event_frame_has_id_event_data_fields(
        self, async_client: AsyncClient, hub: LiveHub
    ) -> None:
        """Normal events have id:, event:, data: in that order."""
        event = _make_reading()
        anchor = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)
        target = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)

        resp = await async_client.get(
            "/api/live/events",
            params={
                "tenant_id": _TENANT,
                "greenhouse_id": _GREENHOUSE,
                "last_event_id": anchor,
            },
            timeout=10.0,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        # Check raw text for ordering: id before event before data
        lines = resp.text.split("\n")
        for i, line in enumerate(lines):
            if line.startswith(f"id: {target}"):
                # Next non-empty line should be event:
                assert lines[i + 1].startswith("event:")
                # Line after that should be data:
                assert lines[i + 2].startswith("data:")
                break
        else:
            pytest.fail(f"Target frame {target} not found in: {resp.text}")

    @pytest.mark.asyncio
    async def test_frame_data_is_valid_json(
        self, async_client: AsyncClient, hub: LiveHub
    ) -> None:
        event = _make_reading()
        anchor = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)
        await hub.publish(_TENANT, _GREENHOUSE, "reading", event)

        resp = await async_client.get(
            "/api/live/events",
            params={
                "tenant_id": _TENANT,
                "greenhouse_id": _GREENHOUSE,
                "last_event_id": anchor,
            },
            timeout=10.0,
        )
        frames = _parse_frames(resp.text)
        data_frames = [f for f in frames if "data" in f and "id" in f]
        assert data_frames
        data = json.loads(data_frames[0]["data"])
        assert "temperature" in str(data)

    @pytest.mark.asyncio
    async def test_response_headers(self, async_client: AsyncClient) -> None:
        resp = await async_client.get(
            "/api/live/events",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
            timeout=10.0,
        )
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["x-accel-buffering"] == "no"


# ---------------------------------------------------------------------------
# Heartbeat rate-limit
# ---------------------------------------------------------------------------


class TestHeartbeat:

    @pytest.mark.asyncio
    async def test_heartbeat_emitted_on_idle(self, async_client: AsyncClient) -> None:
        """First idle tick emits a heartbeat event."""
        resp = await async_client.get(
            "/api/live/events",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
            timeout=10.0,
        )
        frames = _parse_frames(resp.text)
        heartbeats = [f for f in frames if f.get("event") == "heartbeat"]
        assert heartbeats
        data = json.loads(heartbeats[0]["data"])
        assert "ts" in data

    @pytest.mark.asyncio
    async def test_keep_alive_when_heartbeat_not_due(
        self, async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keep-alive comment when heartbeat interval hasn't elapsed."""
        monkeypatch.setattr("src.api.routes.live.LIVE_HEARTBEAT_SECONDS", 9999.0)
        monkeypatch.setattr("src.api.routes.live.LIVE_MAX_STREAM_SECONDS", 0.06)

        resp = await async_client.get(
            "/api/live/events",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
            timeout=10.0,
        )
        assert ": keep-alive" in resp.text


# ---------------------------------------------------------------------------
# Reset frame
# ---------------------------------------------------------------------------


class TestResetFrame:

    @pytest.mark.asyncio
    async def test_reset_on_trimmed_id(
        self, async_client: AsyncClient, hub: LiveHub
    ) -> None:
        """Requesting a trimmed/unknown last_event_id yields a reset frame."""
        await hub.publish(_TENANT, _GREENHOUSE, "reading", _make_reading())

        resp = await async_client.get(
            "/api/live/events",
            params={
                "tenant_id": _TENANT,
                "greenhouse_id": _GREENHOUSE,
                "last_event_id": "0-0",
            },
            timeout=10.0,
        )
        assert "event: reset" in resp.text
        assert "replay-gap" in resp.text


# ---------------------------------------------------------------------------
# Last-Event-ID (header vs query param)
# ---------------------------------------------------------------------------


class TestLastEventID:

    @pytest.mark.asyncio
    async def test_header_takes_precedence_over_query(
        self, async_client: AsyncClient, hub: LiveHub
    ) -> None:
        """Last-Event-ID header overrides last_event_id query param."""
        event = _make_reading()
        id1 = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)
        id2 = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)
        id3 = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)

        resp = await async_client.get(
            "/api/live/events",
            params={
                "tenant_id": _TENANT,
                "greenhouse_id": _GREENHOUSE,
                "last_event_id": id1,  # query param: would replay id2+id3
            },
            headers={"Last-Event-ID": id2},  # header: should replay only id3
            timeout=10.0,
        )
        frames = _parse_frames(resp.text)
        id_frames = [f for f in frames if f.get("id")]
        # Should contain id3 but NOT id2
        ids = [f["id"] for f in id_frames]
        assert id3 in ids
        assert id2 not in ids

    @pytest.mark.asyncio
    async def test_query_param_works(
        self, async_client: AsyncClient, hub: LiveHub
    ) -> None:
        """last_event_id query param replays missed events."""
        event = _make_reading()
        anchor = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)
        target = await hub.publish(_TENANT, _GREENHOUSE, "reading", event)

        resp = await async_client.get(
            "/api/live/events",
            params={
                "tenant_id": _TENANT,
                "greenhouse_id": _GREENHOUSE,
                "last_event_id": anchor,
            },
            timeout=10.0,
        )
        assert target in resp.text


# ---------------------------------------------------------------------------
# Max-stream-seconds deadline
# ---------------------------------------------------------------------------


class TestMaxStreamDeadline:

    @pytest.mark.asyncio
    async def test_stream_closes_after_deadline(self, async_client: AsyncClient) -> None:
        """Stream closes after LIVE_MAX_STREAM_SECONDS."""
        resp = await async_client.get(
            "/api/live/events",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
            timeout=10.0,
        )
        # Response should complete (not hang forever)
        assert resp.status_code == 200
        # Should have at least one frame (heartbeat)
        assert len(resp.text) > 0


# ---------------------------------------------------------------------------
# Auth 401/403
# ---------------------------------------------------------------------------


class TestAuth:

    @pytest.mark.asyncio
    async def test_missing_api_key_401(
        self, hub: LiveHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
    async def test_wrong_tenant_403(
        self, hub: LiveHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    @pytest.mark.asyncio
    async def test_valid_key_200(
        self, hub: LiveHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.config.AUTH_ENABLED", True)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
        register_test_api_key(_TENANT, _API_KEY)

        app.dependency_overrides[get_live_hub] = lambda: hub
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/live/events",
                params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
                headers={"X-API-Key": _API_KEY},
                timeout=10.0,
            )
        app.dependency_overrides.pop(get_live_hub, None)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_status_endpoint_missing_key_401(
        self, hub: LiveHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.config.AUTH_ENABLED", True)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
        register_test_api_key(_TENANT, _API_KEY)

        app.dependency_overrides[get_live_hub] = lambda: hub
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/live/status",
                params={"tenant_id": _TENANT},
                timeout=5.0,
            )
        app.dependency_overrides.pop(get_live_hub, None)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_cross_tenant_rejection_403(
        self, hub: LiveHub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """API key for tenant_A cannot access tenant_B's data."""
        monkeypatch.setattr("src.config.AUTH_ENABLED", True)
        monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
        register_test_api_key("tenant_A", "key_A")

        app.dependency_overrides[get_live_hub] = lambda: hub
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/live/events",
                params={"tenant_id": "tenant_B", "greenhouse_id": _GREENHOUSE},
                headers={"X-API-Key": "key_A"},
                timeout=5.0,
            )
        app.dependency_overrides.pop(get_live_hub, None)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /api/live/status endpoint
# ---------------------------------------------------------------------------


class TestStatusEndpoint:

    @pytest.mark.asyncio
    async def test_status_no_runtime_returns_empty(
        self, async_client: AsyncClient
    ) -> None:
        """When live_runtime is None, status returns empty sources."""
        # Remove any existing runtime from app.state
        if hasattr(app.state, "live_runtime"):
            delattr(app.state, "live_runtime")

        resp = await async_client.get(
            "/api/live/status",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
            timeout=5.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"sources": []}

    @pytest.mark.asyncio
    async def test_status_with_runtime_returns_sources(
        self, async_client: AsyncClient
    ) -> None:
        """When runtime is present, status returns source list with vendor + last_seen."""
        mock_runtime = MagicMock()
        mock_runtime.source_statuses.return_value = [
            {
                "source_id": "climate",
                "vendor": "mock",
                "status": "online",
                "last_seen": None,
            },
            {
                "source_id": "co2",
                "vendor": "mock",
                "status": "degraded",
                "last_seen": "2026-07-19T10:00:00+00:00",
            },
        ]
        app.state.live_runtime = mock_runtime

        resp = await async_client.get(
            "/api/live/status",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
            timeout=5.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sources"]) == 2
        assert data["sources"][0]["source_id"] == "climate"
        assert data["sources"][0]["vendor"] == "mock", "vendor field required by FE schema (N6)"
        assert data["sources"][1]["last_seen"] == "2026-07-19T10:00:00+00:00"

        delattr(app.state, "live_runtime")

    @pytest.mark.asyncio
    async def test_status_unknown_scope_returns_empty(
        self, async_client: AsyncClient
    ) -> None:
        mock_runtime = MagicMock()
        mock_runtime.source_statuses.return_value = []
        app.state.live_runtime = mock_runtime

        resp = await async_client.get(
            "/api/live/status",
            params={"tenant_id": "unknown", "greenhouse_id": "unknown"},
            timeout=5.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"sources": []}

        delattr(app.state, "live_runtime")

"""Tests for POST /api/live/pause and its round trip with /resume.

Covers: pause suppresses the scope gate (mirrors /resume, which already
clears it); pause/resume are idempotent and return 200 when live_runtime is
None; pause/resume delegate to LiveRuntime.stop_scope/start_scope when a
runtime is present; the full lifecycle (task spawn/cancel, not just the
suppression flag) driven end to end through a real LiveRuntime.
"""

from __future__ import annotations

from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from src.api.auth import clear_test_api_keys
from src.api.main import app
from src.live.hub import LiveHub
from src.live.runtime import LiveRuntime
from src.live.scope_control import clear_all_suppressed_scopes, is_live_scope_suppressed
from tests.live.conftest import _safe_redis_close

_TENANT = "tenant_1"
_GREENHOUSE = "gh_A"


@pytest_asyncio.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(autouse=True)
def _reset_auth_keys() -> None:
    clear_test_api_keys()
    yield
    clear_test_api_keys()


@pytest.fixture(autouse=True)
def _reset_scope_gate() -> None:
    clear_all_suppressed_scopes()
    yield
    clear_all_suppressed_scopes()
    if hasattr(app.state, "live_runtime"):
        delattr(app.state, "live_runtime")


class TestPauseResumeRoundTrip:

    @pytest.mark.asyncio
    async def test_pause_suppresses_then_resume_clears(
        self, async_client: AsyncClient
    ) -> None:
        """No runtime on app.state — pause/resume fall back to scope_control directly."""
        assert not is_live_scope_suppressed(_TENANT, _GREENHOUSE)

        resp = await async_client.post(
            "/api/live/pause",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "paused": True,
            "tenant_id": _TENANT,
            "greenhouse_id": _GREENHOUSE,
        }
        assert is_live_scope_suppressed(_TENANT, _GREENHOUSE)

        resp = await async_client.post(
            "/api/live/resume",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json()["resumed"] is True
        assert not is_live_scope_suppressed(_TENANT, _GREENHOUSE)

    @pytest.mark.asyncio
    async def test_pause_idempotent_when_runtime_none(
        self, async_client: AsyncClient
    ) -> None:
        for _ in range(2):
            resp = await async_client.post(
                "/api/live/pause",
                params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
            )
            assert resp.status_code == 200
            assert resp.json()["paused"] is True
        assert is_live_scope_suppressed(_TENANT, _GREENHOUSE)

    @pytest.mark.asyncio
    async def test_pause_delegates_to_runtime_when_present(
        self, async_client: AsyncClient
    ) -> None:
        mock_runtime = MagicMock()
        mock_runtime.stop_scope = AsyncMock(return_value=True)
        app.state.live_runtime = mock_runtime

        resp = await async_client.post(
            "/api/live/pause",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json()["paused"] is True
        mock_runtime.stop_scope.assert_awaited_once_with(_TENANT, _GREENHOUSE)
        # Routed through the runtime, not scope_control directly.
        assert not is_live_scope_suppressed(_TENANT, _GREENHOUSE)

    @pytest.mark.asyncio
    async def test_resume_delegates_to_runtime_when_present(
        self, async_client: AsyncClient
    ) -> None:
        mock_runtime = MagicMock()
        mock_runtime.start_scope = AsyncMock(return_value=True)
        app.state.live_runtime = mock_runtime

        resp = await async_client.post(
            "/api/live/resume",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json()["resumed"] is True
        mock_runtime.start_scope.assert_awaited_once_with(_TENANT, _GREENHOUSE)


class TestPauseResumeDriveRealRuntimeLifecycle:
    """End to end: /resume and /pause must actually start and stop a
    scope's tasks on a real LiveRuntime, not just flip the suppression flag —
    that is the whole point of the fix this test guards."""

    @pytest_asyncio.fixture
    async def fake_redis(self) -> AsyncGenerator[FakeRedis, None]:
        client = FakeRedis()
        yield client
        await _safe_redis_close(client)

    @pytest_asyncio.fixture
    async def real_runtime(
        self, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncGenerator[LiveRuntime, None]:
        monkeypatch.setattr("src.live.runtime_builder.MOCK_SCADA_ENABLED", True)
        monkeypatch.setattr(
            "src.live.runtime_builder.MOCK_SCADA_SCOPES", f"{_TENANT}:{_GREENHOUSE}"
        )
        monkeypatch.setattr("src.live.runtime_builder.MOCK_SCADA_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr("src.live.sources.mock_factory.MOCK_SCADA_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr("src.live.runtime_builder.LIVE_PERSIST_ENABLED", False)
        monkeypatch.setattr("src.live.runtime_builder.LIVE_SOURCES_JSON", "")

        hub = LiveHub(redis_client=fake_redis, block_ms=10)
        runtime = LiveRuntime.build_from_config(hub)
        yield runtime
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_resume_then_pause_starts_and_stops_scope_tasks(
        self, async_client: AsyncClient, real_runtime: LiveRuntime
    ) -> None:
        app.state.live_runtime = real_runtime
        assert not real_runtime.is_scope_active(_TENANT, _GREENHOUSE)

        resp = await async_client.post(
            "/api/live/resume",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert real_runtime.is_scope_active(_TENANT, _GREENHOUSE)
        assert not is_live_scope_suppressed(_TENANT, _GREENHOUSE)

        resp = await async_client.post(
            "/api/live/pause",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert not real_runtime.is_scope_active(_TENANT, _GREENHOUSE)
        assert is_live_scope_suppressed(_TENANT, _GREENHOUSE)

"""Tests for POST /api/live/pause and its round trip with /resume.

Covers: pause suppresses the scope gate (mirrors /resume, which already
clears it); pause/resume are idempotent and return 200 when live_runtime is
None; pause/resume delegate to LiveRuntime.stop_scope/start_scope when a
runtime is present; the full lifecycle (task spawn/cancel, not just the
suppression flag) driven end to end through a real LiveRuntime; and the
``scope_status`` discriminator that tells "not configured" (live will never
flow) apart from "already running" (live is flowing, this was a no-op) —
the regression this endpoint previously collapsed into a blanket
``resumed: true``.
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
_UNCONFIGURED_GREENHOUSE = "gh_never_configured"


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
        """No runtime on app.state — pause/resume fall back to scope_control
        directly, which can't tell configured scopes apart, hence "unmanaged"."""
        assert not is_live_scope_suppressed(_TENANT, _GREENHOUSE)

        resp = await async_client.post(
            "/api/live/pause",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "paused": True,
            "scope_status": "unmanaged",
            "tenant_id": _TENANT,
            "greenhouse_id": _GREENHOUSE,
        }
        assert is_live_scope_suppressed(_TENANT, _GREENHOUSE)

        resp = await async_client.post(
            "/api/live/resume",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "resumed": True,
            "scope_status": "unmanaged",
            "tenant_id": _TENANT,
            "greenhouse_id": _GREENHOUSE,
        }
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
            assert resp.json()["scope_status"] == "unmanaged"
        assert is_live_scope_suppressed(_TENANT, _GREENHOUSE)

    @pytest.mark.asyncio
    async def test_pause_delegates_to_runtime_when_present(
        self, async_client: AsyncClient
    ) -> None:
        mock_runtime = MagicMock()
        mock_runtime.is_scope_configured = MagicMock(return_value=True)
        mock_runtime.stop_scope = AsyncMock(return_value=True)
        app.state.live_runtime = mock_runtime

        resp = await async_client.post(
            "/api/live/pause",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json()["paused"] is True
        assert resp.json()["scope_status"] == "stopped"
        mock_runtime.stop_scope.assert_awaited_once_with(_TENANT, _GREENHOUSE)
        # Routed through the runtime, not scope_control directly.
        assert not is_live_scope_suppressed(_TENANT, _GREENHOUSE)

    @pytest.mark.asyncio
    async def test_resume_delegates_to_runtime_when_present(
        self, async_client: AsyncClient
    ) -> None:
        mock_runtime = MagicMock()
        mock_runtime.is_scope_configured = MagicMock(return_value=True)
        mock_runtime.start_scope = AsyncMock(return_value=True)
        app.state.live_runtime = mock_runtime

        resp = await async_client.post(
            "/api/live/resume",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json()["resumed"] is True
        assert resp.json()["scope_status"] == "started"
        mock_runtime.start_scope.assert_awaited_once_with(_TENANT, _GREENHOUSE)

    @pytest.mark.asyncio
    async def test_resume_on_unconfigured_scope_reports_not_configured(
        self, async_client: AsyncClient
    ) -> None:
        """The regression this fix exists for: an unconfigured scope must not
        report resumed: true — no data will ever flow no matter the toggle."""
        mock_runtime = MagicMock()
        mock_runtime.is_scope_configured = MagicMock(return_value=False)
        mock_runtime.start_scope = AsyncMock(return_value=False)
        app.state.live_runtime = mock_runtime

        resp = await async_client.post(
            "/api/live/resume",
            params={"tenant_id": _TENANT, "greenhouse_id": _UNCONFIGURED_GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "resumed": False,
            "scope_status": "not_configured",
            "tenant_id": _TENANT,
            "greenhouse_id": _UNCONFIGURED_GREENHOUSE,
        }
        # start_scope must not even be called — is_scope_configured short-circuits.
        mock_runtime.start_scope.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pause_on_unconfigured_scope_reports_not_configured(
        self, async_client: AsyncClient
    ) -> None:
        """paused stays True (nothing streams either way) but scope_status
        says there was nothing to stop."""
        mock_runtime = MagicMock()
        mock_runtime.is_scope_configured = MagicMock(return_value=False)
        mock_runtime.stop_scope = AsyncMock(return_value=False)
        app.state.live_runtime = mock_runtime

        resp = await async_client.post(
            "/api/live/pause",
            params={"tenant_id": _TENANT, "greenhouse_id": _UNCONFIGURED_GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "paused": True,
            "scope_status": "not_configured",
            "tenant_id": _TENANT,
            "greenhouse_id": _UNCONFIGURED_GREENHOUSE,
        }
        mock_runtime.stop_scope.assert_not_awaited()


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
        assert resp.json()["scope_status"] == "started"
        assert real_runtime.is_scope_active(_TENANT, _GREENHOUSE)
        assert not is_live_scope_suppressed(_TENANT, _GREENHOUSE)

        resp = await async_client.post(
            "/api/live/pause",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json()["scope_status"] == "stopped"
        assert not real_runtime.is_scope_active(_TENANT, _GREENHOUSE)
        assert is_live_scope_suppressed(_TENANT, _GREENHOUSE)

    @pytest.mark.asyncio
    async def test_resume_called_twice_second_call_is_already_running(
        self, async_client: AsyncClient, real_runtime: LiveRuntime
    ) -> None:
        """This is the regression this design exists to prevent: a second
        /resume on a scope that is already live must NOT report resumed:
        false — the operator's toggle is already correctly on."""
        app.state.live_runtime = real_runtime

        first = await async_client.post(
            "/api/live/resume",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert first.json()["resumed"] is True
        assert first.json()["scope_status"] == "started"

        second = await async_client.post(
            "/api/live/resume",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert second.json()["resumed"] is True
        assert second.json()["scope_status"] == "already_running"
        assert real_runtime.is_scope_active(_TENANT, _GREENHOUSE)

    @pytest.mark.asyncio
    async def test_pause_on_idle_configured_scope_is_not_running(
        self, async_client: AsyncClient, real_runtime: LiveRuntime
    ) -> None:
        """A configured scope that was never resumed — pausing it is a
        harmless idempotent no-op, not an error."""
        app.state.live_runtime = real_runtime
        assert not real_runtime.is_scope_active(_TENANT, _GREENHOUSE)

        resp = await async_client.post(
            "/api/live/pause",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json()["paused"] is True
        assert resp.json()["scope_status"] == "not_running"

    @pytest.mark.asyncio
    async def test_resume_on_unconfigured_scope_never_starts_tasks(
        self, async_client: AsyncClient, real_runtime: LiveRuntime
    ) -> None:
        """A real runtime, but a greenhouse outside MOCK_SCADA_SCOPES —
        resumed must be false and no tasks may be spawned."""
        app.state.live_runtime = real_runtime

        resp = await async_client.post(
            "/api/live/resume",
            params={"tenant_id": _TENANT, "greenhouse_id": _UNCONFIGURED_GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "resumed": False,
            "scope_status": "not_configured",
            "tenant_id": _TENANT,
            "greenhouse_id": _UNCONFIGURED_GREENHOUSE,
        }
        assert not real_runtime.is_scope_active(_TENANT, _UNCONFIGURED_GREENHOUSE)

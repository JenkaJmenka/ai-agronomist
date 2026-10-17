"""Smoke tests for LiveRuntime (Phase 3B).

Coverage
--------
- build_from_config with a mock scope + fakeredis hub builds correctly.
- start() + brief run: at least 1 ``reading`` event is published to the hub.
- stop() cancels all tasks cleanly (no lingering tasks, no CancelledError leaks).
- source_statuses() returns a list of dicts for the configured scope.
- source_statuses() returns [] for unknown scopes.
- Scorer passthrough: when LiveScorer is unavailable the runtime still publishes.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from src.live.events import LiveReadingEvent
from src.live.hub import LiveHub
from src.live.runtime import LiveRuntime
from tests.live.conftest import _safe_redis_close

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT = "rt_tenant"
_GH = "rt_gh"
_SCOPE = f"{_TENANT}:{_GH}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    client = FakeRedis()
    yield client
    await _safe_redis_close(client)


@pytest_asyncio.fixture
def hub(fake_redis: FakeRedis) -> LiveHub:
    return LiveHub(redis_client=fake_redis, block_ms=10)


@pytest.fixture(autouse=True)
def _mock_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin config values to keep tests fast and deterministic."""
    monkeypatch.setattr("src.live.runtime_builder.MOCK_SCADA_ENABLED", True)
    monkeypatch.setattr("src.live.runtime_builder.MOCK_SCADA_SCOPES", _SCOPE)
    # Tiny interval so readings arrive quickly.
    monkeypatch.setattr("src.live.runtime_builder.MOCK_SCADA_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr("src.config.MOCK_SCADA_INTERVAL_SECONDS", 0.05)
    # Also patch the mock source module so build_mock_sources_for_scope creates
    # SourceConfig objects with poll_interval_seconds=0.05.
    # The runtime now derives emit_interval from source_configs.poll_interval_seconds,
    # so all three namespaces must agree for fast tests.
    monkeypatch.setattr("src.live.sources.mock_factory.MOCK_SCADA_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr("src.live.runtime_builder.LIVE_PERSIST_ENABLED", False)
    monkeypatch.setattr("src.live.runtime_builder.LIVE_SOURCES_JSON", "")


@pytest.fixture(autouse=True)
def _block_live_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the lazy LiveScorer import fail so we test the passthrough path.

    We insert a sentinel module object that raises ImportError when
    ``from src.live.scoring import LiveScorer`` is attempted inside
    ``build_from_config``.
    """
    # Remove a cached real module if present, then insert a broken sentinel.
    sys.modules.pop("src.live.scoring", None)
    broken = MagicMock()
    broken.__spec__ = None  # mark as not a real spec

    class _FailImport:
        def __getattr__(self, name: str):
            raise ImportError("LiveScorer not available in test")

    sys.modules["src.live.scoring"] = _FailImport()  # type: ignore[assignment]
    yield
    sys.modules.pop("src.live.scoring", None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_stream_entry(
    fake_redis: FakeRedis,
    tenant: str,
    gh: str,
    *,
    timeout: float = 3.0,
    event_type: str = "reading",
) -> bool:
    """Poll the Redis stream until a reading entry appears or timeout elapses."""
    from src.live.hub import stream_key
    key = stream_key(tenant, gh)
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        entries = await fake_redis.xrange(key, "-", "+")
        for _eid, fields in entries:
            t = fields.get(b"type", b"").decode()
            if t == event_type:
                return True
        await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_from_config_creates_scope(hub: LiveHub) -> None:
    """build_from_config creates one scope for the configured MOCK_SCADA_SCOPES."""
    runtime = LiveRuntime.build_from_config(hub)
    assert len(runtime._scopes) == 1
    scope = runtime._scopes[0]
    assert scope.tenant_id == _TENANT
    assert scope.greenhouse_id == _GH
    assert len(scope.connectors) > 0


@pytest.mark.asyncio
async def test_readings_published_to_hub(
    hub: LiveHub, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After start(), at least one reading event is published within 3 s.

    Idle-by-default (LIVE_AUTOSTART_ENABLED=False) spawns no scope tasks, so
    opt in explicitly to exercise the streaming path.
    """
    monkeypatch.setattr("src.live.runtime.LIVE_AUTOSTART_ENABLED", True)
    runtime = LiveRuntime.build_from_config(hub)
    await runtime.start()
    try:
        found = await _wait_for_stream_entry(fake_redis, _TENANT, _GH, timeout=3.0)
        assert found, "Expected at least one 'reading' event in the Redis stream"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_stop_cancels_all_tasks_cleanly(
    hub: LiveHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop() cancels all tasks without leaving pending tasks or raising."""
    monkeypatch.setattr("src.live.runtime.LIVE_AUTOSTART_ENABLED", True)
    runtime = LiveRuntime.build_from_config(hub)
    await runtime.start()
    assert runtime._all_tasks(), "Expected at least one task after start()"

    await runtime.stop()

    # All tasks must be done after stop().
    assert not runtime._all_tasks(), "Expected task list to be cleared after stop()"


@pytest.mark.asyncio
async def test_source_statuses_returns_list(hub: LiveHub) -> None:
    """source_statuses() returns a non-empty list of dicts for the known scope."""
    runtime = LiveRuntime.build_from_config(hub)
    await runtime.start()
    await asyncio.sleep(0.1)  # brief run to ensure merger health is tracked

    statuses = runtime.source_statuses(_TENANT, _GH)
    assert isinstance(statuses, list)
    assert len(statuses) > 0, "Expected at least one source status entry"
    for entry in statuses:
        assert "source_id" in entry
        assert "vendor" in entry, "source_statuses() must include 'vendor' (N6 fix)"
        assert "status" in entry
        assert "last_seen" in entry
        # "paused" included: this scope was never started via start_scope, and
        # an idle scope must not report the merger's seeded "online".
        assert entry["status"] in {"online", "degraded", "offline", "paused"}

    await runtime.stop()


@pytest.mark.asyncio
async def test_source_statuses_unknown_scope_returns_empty(hub: LiveHub) -> None:
    """source_statuses() returns [] for a scope not known to the runtime."""
    runtime = LiveRuntime.build_from_config(hub)
    result = runtime.source_statuses("no_such_tenant", "no_such_gh")
    assert result == []


@pytest.mark.asyncio
async def test_scorer_passthrough_when_unavailable(
    hub: LiveHub, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When LiveScorer is unavailable, readings are still published (passthrough)."""
    monkeypatch.setattr("src.live.runtime.LIVE_AUTOSTART_ENABLED", True)
    runtime = LiveRuntime.build_from_config(hub)
    # Scorer should be None since we injected the broken sentinel.
    assert runtime._scopes[0].scorer is None

    await runtime.start()
    try:
        found = await _wait_for_stream_entry(fake_redis, _TENANT, _GH, timeout=3.0)
        assert found, "Readings should be published even without a scorer"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_start_stop_idempotent_no_pending_tasks(
    hub: LiveHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After stop(), no asyncio tasks created by the runtime are still pending."""
    monkeypatch.setattr("src.live.runtime.LIVE_AUTOSTART_ENABLED", True)
    runtime = LiveRuntime.build_from_config(hub)
    await runtime.start()
    task_names = {t.get_name() for t in runtime._all_tasks()}

    await runtime.stop()

    # All tasks from this runtime must be done.
    for t in asyncio.all_tasks():
        if t.get_name() in task_names:
            assert t.done(), f"Task {t.get_name()!r} is still pending after stop()"

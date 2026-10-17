"""Tests for the empty-tenant P1 bug: all three discriminated live-status states.

Bug reproduced in production:
  GET /api/readings/greenhouses?tenant_id=tenant_b  → {'greenhouses':[]}
  GET /api/live/status?tenant_id=tenant_b&greenhouse_id=gh_A → {'sources':[]}

All three states previously returned identical {"sources": []} HTTP 200,
making them indistinguishable at the client. This file verifies that the
fixed API is distinguishable:

  (a) greenhouse does not exist   → greenhouse_status: "not_registered"
  (b) greenhouse exists, no source → greenhouse_status: "no_source"
  (c) source attached (live scope) → greenhouse_status: "live"
"""

from __future__ import annotations

from typing import AsyncGenerator
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.api.services.live_hub_provider import get_live_hub
from src.db.models import Base, Greenhouse, RawReading, SensorReading
from src.db.session import get_db
from src.live.hub import LiveHub

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT = "demo_tenant"
_GREENHOUSE = "gh_A"
_OTHER_TENANT = "other_tenant"
_API_KEY = "test-empty-tenant-key"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def test_db():
    """In-memory SQLite with full schema; yields an open AsyncSession."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    client = FakeRedis()
    yield client
    close_fn = getattr(client, "aclose", None) or client.close
    await close_fn()


@pytest_asyncio.fixture
async def hub(fake_redis: FakeRedis) -> LiveHub:
    return LiveHub(redis_client=fake_redis, block_ms=10)


@pytest_asyncio.fixture
async def async_client(test_db: AsyncSession, hub: LiveHub) -> AsyncGenerator[AsyncClient, None]:
    """Override both DB and hub; no live_runtime on app.state."""
    app.dependency_overrides[get_db] = lambda: test_db
    app.dependency_overrides[get_live_hub] = lambda: hub
    if hasattr(app.state, "live_runtime"):
        delattr(app.state, "live_runtime")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_live_hub, None)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _get_status(client: AsyncClient, tenant: str, greenhouse: str) -> dict:
    resp = await client.get(
        "/api/live/status",
        params={"tenant_id": tenant, "greenhouse_id": greenhouse},
        timeout=5.0,
    )
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Task 1: greenhouse registry decoupled from readings
# ---------------------------------------------------------------------------


class TestGreenhouseRegistry:
    """Registered greenhouses appear in GET /greenhouses with zero readings."""

    @pytest.mark.asyncio
    async def test_registered_greenhouse_appears_with_zero_readings(
        self, async_client: AsyncClient, test_db: AsyncSession
    ) -> None:
        """POST /greenhouses registers a greenhouse that has no readings yet."""
        # No readings exist for demo_tenant — greenhouses list must be empty before.
        resp = await async_client.get(
            "/api/readings/greenhouses", params={"tenant_id": _TENANT}
        )
        assert resp.json()["greenhouses"] == []

        # Register via the new endpoint.
        resp = await async_client.post(
            "/api/readings/greenhouses",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] is True
        assert body["greenhouse_id"] == _GREENHOUSE

        # Now the greenhouse must appear even though there are still no readings.
        resp = await async_client.get(
            "/api/readings/greenhouses", params={"tenant_id": _TENANT}
        )
        assert _GREENHOUSE in resp.json()["greenhouses"]

    @pytest.mark.asyncio
    async def test_register_greenhouse_is_idempotent(
        self, async_client: AsyncClient
    ) -> None:
        """Calling register twice for the same pair is safe; second call returns created=false."""
        await async_client.post(
            "/api/readings/greenhouses",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        resp = await async_client.post(
            "/api/readings/greenhouses",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        assert resp.status_code == 200
        assert resp.json()["created"] is False

    @pytest.mark.asyncio
    async def test_greenhouses_from_readings_still_appear(
        self, async_client: AsyncClient, test_db: AsyncSession
    ) -> None:
        """Tenants with readings (but no registry row) still return their greenhouses."""
        import datetime

        ts = datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc)
        test_db.add(
            RawReading(
                tenant_id=_OTHER_TENANT,
                greenhouse_id="gh_B",
                timestamp=ts,
                source="csv",
                data={"temperature": 22.0},
            )
        )
        await test_db.commit()

        resp = await async_client.get(
            "/api/readings/greenhouses", params={"tenant_id": _OTHER_TENANT}
        )
        assert "gh_B" in resp.json()["greenhouses"]

    @pytest.mark.asyncio
    async def test_greenhouses_merges_registry_and_readings(
        self, async_client: AsyncClient, test_db: AsyncSession
    ) -> None:
        """When a tenant has both a registry row and readings, both appear merged."""
        import datetime

        # Register gh_A (no readings).
        await async_client.post(
            "/api/readings/greenhouses",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        # Add readings for a different greenhouse.
        ts = datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc)
        test_db.add(
            RawReading(
                tenant_id=_TENANT,
                greenhouse_id="gh_B",
                timestamp=ts,
                source="csv",
                data={"temperature": 22.0},
            )
        )
        await test_db.commit()

        resp = await async_client.get(
            "/api/readings/greenhouses", params={"tenant_id": _TENANT}
        )
        greenhouses = resp.json()["greenhouses"]
        assert _GREENHOUSE in greenhouses, "registry row must be included"
        assert "gh_B" in greenhouses, "readings-based greenhouse must be included"
        assert greenhouses == sorted(greenhouses), "result must be sorted"


# ---------------------------------------------------------------------------
# Task 2: discriminated status states
# ---------------------------------------------------------------------------


class TestLiveStatusDiscrimination:
    """The three empty-source states must return distinct greenhouse_status values."""

    @pytest.mark.asyncio
    async def test_state_a_not_registered(self, async_client: AsyncClient) -> None:
        """(a) Greenhouse not in registry or readings → 'not_registered'."""
        data = await _get_status(async_client, _TENANT, _GREENHOUSE)
        assert data["greenhouse_status"] == "not_registered"
        assert data["sources"] == []

    @pytest.mark.asyncio
    async def test_state_b_no_source_greenhouse_in_registry(
        self, async_client: AsyncClient
    ) -> None:
        """(b) Greenhouse registered but runtime not watching → 'no_source'."""
        # Register the greenhouse (no readings, no runtime scope).
        await async_client.post(
            "/api/readings/greenhouses",
            params={"tenant_id": _TENANT, "greenhouse_id": _GREENHOUSE},
        )
        data = await _get_status(async_client, _TENANT, _GREENHOUSE)
        assert data["greenhouse_status"] == "no_source"
        assert data["sources"] == []

    @pytest.mark.asyncio
    async def test_state_b_no_source_greenhouse_has_readings(
        self, async_client: AsyncClient, test_db: AsyncSession
    ) -> None:
        """(b) Greenhouse with readings but runtime not watching → 'no_source'."""
        import datetime

        ts = datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc)
        test_db.add(
            RawReading(
                tenant_id=_TENANT,
                greenhouse_id=_GREENHOUSE,
                timestamp=ts,
                source="csv",
                data={"temperature": 22.0},
            )
        )
        await test_db.commit()

        data = await _get_status(async_client, _TENANT, _GREENHOUSE)
        assert data["greenhouse_status"] == "no_source"
        assert data["sources"] == []

    @pytest.mark.asyncio
    async def test_state_c_live_with_sources(
        self, async_client: AsyncClient
    ) -> None:
        """(c) Runtime has scope for greenhouse → 'live', sources present."""
        mock_runtime = MagicMock()
        mock_runtime.source_statuses.return_value = [
            {
                "source_id": "climate",
                "vendor": "mock",
                "status": "online",
                "last_seen": None,  # warming up — no data yet
            }
        ]
        app.state.live_runtime = mock_runtime
        try:
            data = await _get_status(async_client, _TENANT, _GREENHOUSE)
            assert data["greenhouse_status"] == "live"
            # sources preserved — existing consumers unaffected
            assert len(data["sources"]) == 1
            assert data["sources"][0]["source_id"] == "climate"
            assert data["sources"][0]["last_seen"] is None
        finally:
            if hasattr(app.state, "live_runtime"):
                delattr(app.state, "live_runtime")

    @pytest.mark.asyncio
    async def test_detector_state_reports_missing_so_scope_is_not_healthy(
        self, async_client: AsyncClient
    ) -> None:
        """A live scope with no model must say so, not look fine.

        This is the operator-visible half of the no-detector fix: readings keep
        persisting, but the UI must be able to render "live, not analysed".
        """
        mock_runtime = MagicMock()
        mock_runtime.source_statuses.return_value = [
            {
                "source_id": "climate",
                "vendor": "mock",
                "status": "online",
                "last_seen": None,
            }
        ]
        mock_runtime.detector_state.return_value = "missing"
        app.state.live_runtime = mock_runtime
        try:
            data = await _get_status(async_client, _TENANT, _GREENHOUSE)
            assert data["greenhouse_status"] == "live"
            assert data["detector_state"] == "missing", (
                "a live scope with no deployed model must report "
                "detector_state='missing'"
            )
        finally:
            if hasattr(app.state, "live_runtime"):
                delattr(app.state, "live_runtime")

    @pytest.mark.asyncio
    async def test_detector_state_defaults_to_unknown_without_a_runtime(
        self, async_client: AsyncClient
    ) -> None:
        """No runtime (readings-service fallback) → 'unknown', still HTTP 200."""
        data = await _get_status(async_client, "no-such-tenant", "no-such-gh")
        assert data["detector_state"] == "unknown"

    @pytest.mark.asyncio
    async def test_detector_state_coerces_an_unrecognised_value(
        self, async_client: AsyncClient
    ) -> None:
        """The route duck-types the runtime, so it validates what it gets back.

        /api/live/status promises HTTP 200 with a valid body; a runtime that
        does not implement detector_state must not turn that into a 500.
        """
        mock_runtime = MagicMock()
        mock_runtime.source_statuses.return_value = []
        mock_runtime.detector_state.return_value = "not-a-real-state"
        app.state.live_runtime = mock_runtime
        try:
            data = await _get_status(async_client, _TENANT, _GREENHOUSE)
            assert data["detector_state"] == "unknown"
        finally:
            if hasattr(app.state, "live_runtime"):
                delattr(app.state, "live_runtime")

    @pytest.mark.asyncio
    async def test_sources_field_always_present(self, async_client: AsyncClient) -> None:
        """'sources' key is always in the response regardless of state."""
        data = await _get_status(async_client, "no-such-tenant", "no-such-gh")
        assert "sources" in data
        assert isinstance(data["sources"], list)

    @pytest.mark.asyncio
    async def test_greenhouse_status_field_always_present(
        self, async_client: AsyncClient
    ) -> None:
        """'greenhouse_status' key is always in the response."""
        data = await _get_status(async_client, "no-such-tenant", "no-such-gh")
        assert "greenhouse_status" in data
        assert data["greenhouse_status"] in ("not_registered", "no_source", "live")


# ---------------------------------------------------------------------------
# Task 3: live ingestion can start with zero historical readings
# ---------------------------------------------------------------------------


class _NullSession:
    """Async-CM stand-in for AsyncSessionLocal() in resolver unit tests."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _null_session_factory():
    return _NullSession()


class TestLiveIngestionWithZeroReadings:
    """Verify live ingestion path makes no assumptions about prior data."""

    @pytest.mark.asyncio
    async def test_detector_resolver_reports_missing_without_prior_data(
        self,
    ) -> None:
        """A tenant with no deployed model resolves to None, not a baseline.

        This test previously asserted the opposite — that resolve() falls back
        to ``loader(None)``, "the baseline detector handles new tenants". That
        premise was false in every environment: ``loader(None)`` builds
        ``AnomalyDetector(None)``, which resolved to ``models/baseline.pt``, a
        path excluded from the image by ``.dockerignore`` and present nowhere.
        The test passed only because it injected a mock loader, so it encoded
        the bug it was meant to guard against.

        Correct behaviour: no detector, state ``missing``, and live ingestion
        still starts — the scorer persists readings unscored.
        """
        from src.api.services.detector_cache import NoActiveModelError
        from src.live.detector_resolver import (
            DETECTOR_STATE_MISSING,
            DetectorResolver,
        )

        loader_calls: list[str | None] = []

        def loader(cp: str | None):
            loader_calls.append(cp)
            return MagicMock()

        async def no_active_model(session, tenant_id):  # noqa: ANN001
            raise NoActiveModelError(f"no active model for {tenant_id}")

        resolver = DetectorResolver(
            "demo_tenant",
            session_factory=_null_session_factory,
            checkpoint_resolver=no_active_model,
            detector_loader=loader,
        )

        result = await resolver.resolve()

        assert result is None, (
            "resolve() must return None when no model is deployed — there is "
            "no filesystem baseline to fall back to"
        )
        assert loader_calls == [], (
            "the detector loader must never be called with None: that is the "
            "dead models/baseline.pt path"
        )
        assert resolver.state == DETECTOR_STATE_MISSING

    @pytest.mark.asyncio
    async def test_detector_resolver_does_not_retry_on_every_event(self) -> None:
        """A missing model is negative-cached, not re-resolved per event.

        Before this, resolve() stamped ``_resolved_at`` only on success, so a
        scope with no model re-ran the registry lookup and another torch.load
        for every single event — measured at 23.7 resolutions/min and 47.3
        torch.load attempts/min on a 2s feed.
        """
        from src.api.services.detector_cache import NoActiveModelError
        from src.live.detector_resolver import DetectorResolver

        attempts: list[str] = []

        async def no_active_model(session, tenant_id):  # noqa: ANN001
            attempts.append(tenant_id)
            raise NoActiveModelError("none")

        resolver = DetectorResolver(
            "demo_tenant",
            session_factory=_null_session_factory,
            checkpoint_resolver=no_active_model,
            detector_loader=lambda cp: MagicMock(),
        )

        for _ in range(25):
            assert await resolver.resolve() is None

        assert len(attempts) == 1, (
            f"expected one resolution attempt inside the retry window, got "
            f"{len(attempts)} — the negative cache is not holding"
        )

    @pytest.mark.asyncio
    async def test_detector_resolver_logs_once_per_state_change(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The missing-model warning is emitted once per scope, not per event."""
        import logging

        from src.api.services.detector_cache import NoActiveModelError
        from src.live.detector_resolver import DetectorResolver

        async def no_active_model(session, tenant_id):  # noqa: ANN001
            raise NoActiveModelError("none")

        resolver = DetectorResolver(
            "demo_tenant",
            session_factory=_null_session_factory,
            checkpoint_resolver=no_active_model,
            detector_loader=lambda cp: MagicMock(),
        )

        with caplog.at_level(logging.WARNING, logger="src.live.detector_resolver"):
            for _ in range(10):
                await resolver.resolve()
            # Defeat the negative cache so resolution is genuinely re-attempted
            # while the state stays 'missing'.
            for _ in range(10):
                resolver._attempted_at = 0.0
                await resolver.resolve()

        missing_warnings = [
            r for r in caplog.records if "No detector deployed" in r.getMessage()
        ]
        assert len(missing_warnings) == 1, (
            f"expected exactly one missing-detector warning per scope, got "
            f"{len(missing_warnings)}"
        )

    @pytest.mark.asyncio
    async def test_live_scorer_warm_up_needs_only_live_data(self) -> None:
        """LiveScorer does not query the DB for historical data on warm-up.

        The scorer only needs SEQ_LEN live ticks to warm up its sliding window.
        This confirms that an empty-readings greenhouse does not block scoring.
        """
        from datetime import datetime, timezone

        from src.live.events import LiveReadingEvent
        from src.live.hub import LiveHub
        from src.live.scoring import LiveScorer

        fake_redis = FakeRedis()
        hub = LiveHub(redis_client=fake_redis, block_ms=10)

        # Resolver that never hits the DB — returns None (no detector) so we
        # can verify the pre-warm-up path without a real checkpoint.
        from src.live.detector_resolver import DetectorResolver

        async def null_resolve(self):  # noqa: ANN001
            return None

        resolver = DetectorResolver("demo_tenant")

        import unittest.mock as mock

        with mock.patch.object(DetectorResolver, "resolve", null_resolve):
            scorer = LiveScorer(
                tenant_id="demo_tenant",
                greenhouse_id="gh_A",
                hub=hub,
                resolver=resolver,
            )

            ts = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
            event = LiveReadingEvent(
                seq="",
                timestamp=ts,
                tenant_id="demo_tenant",
                greenhouse_id="gh_A",
                channels={
                    "temperature": 22.5,
                    "humidity": 65.0,
                    "co2": 420.0,
                    "light": 300.0,
                },
                sources={
                    "temperature": "mock",
                    "humidity": "mock",
                    "co2": "mock",
                    "light": "mock",
                },
            )

            # Should not raise even with zero historical readings — scorer just
            # returns the event unchanged while the window is not yet warm.
            scored = await scorer.score(event)
            assert scored is not None, "scorer must not crash on a fresh greenhouse"

        close_fn = getattr(fake_redis, "aclose", None) or fake_redis.close
        await close_fn()

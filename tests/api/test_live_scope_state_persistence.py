"""Durable Go Live state: survives a restart, and a resumed scope really writes.

Covers the backend half of the live-feed fix:

* ``live_scope_state`` round trip (store / fetch / upsert / missing table).
* Boot resume — stored-live scopes start, unknown scopes stay idle, a scope
  that dropped out of ``MOCK_SCADA_SCOPES`` is named rather than started.
* A tenant data wipe wins over a prior Go Live.
* ``LIVE_AUTOSTART_ENABLED=false`` semantics survive: nothing starts for a
  scope nobody turned on.
* The freshness block on ``GET /api/live/status``.

The central case is :class:`TestResumedScopeWritesRows`. A production audit
found **zero** ``source='live'`` rows for any tenant, so proving that state
survives a restart proves nothing on its own — the test drives a real
``LiveRuntime`` with a real ``PersistenceBatcher`` through the boot-resume
path and asserts rows land in **both** reading tables.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.auth import clear_test_api_keys
from src.api.main import app
from src.api.services import live_boot_resume, live_freshness
from src.api.services.live_boot_resume import (
    log_boot_resume,
    resume_persisted_live_scopes,
)
from src.api.services.live_scope_state_service import (
    DesiredScopeState,
    fetch_desired_state,
    fetch_live_scopes,
    store_desired_state,
)
from src.api.services.tenant_data_wipe import wipe_tenant_data
from src.db.models import Base, LiveScopeState, RawReading, SensorReading
from src.db.session import get_db
from src.live.hub import LiveHub
from src.live.persistence import PersistenceBatcher
from src.live.runtime import LiveRuntime
from src.live.runtime_builder import _build_scope
from src.live.scope_control import clear_all_suppressed_scopes
from src.live.sources.base import SourceConfig
from src.live.sources.mock import MockScadaSource

_TENANT = "tenant_persist"
_GH = "gh_A"
_OTHER_GH = "gh_B"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker, None]:
    """File-less SQLite with the full ORM schema, shared across sessions.

    A single in-memory connection is reused (``StaticPool`` semantics via one
    engine) so the batcher's session and the assertion session see the same
    database.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=None,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def db(session_factory: async_sessionmaker) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def unmigrated_db() -> AsyncGenerator[AsyncSession, None]:
    """A DB where ``live_scope_state`` does not exist.

    Mirrors the deploy-skew hazard: only the API runs ``alembic upgrade
    head``, so newer code can meet an older schema.
    """
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[SensorReading.__table__, RawReading.__table__],
            )
        )
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_scope_gate() -> None:
    clear_all_suppressed_scopes()
    clear_test_api_keys()
    yield
    clear_all_suppressed_scopes()
    clear_test_api_keys()
    if hasattr(app.state, "live_runtime"):
        delattr(app.state, "live_runtime")


def _fake_runtime(configured: list[tuple[str, str]]) -> MagicMock:
    """A LiveRuntime stand-in that reports a fixed set of configured scopes."""
    runtime = MagicMock()
    runtime.configured_scopes.return_value = list(configured)
    runtime.is_scope_configured.side_effect = lambda t, g: (t, g) in configured
    runtime.is_scope_active.return_value = False
    runtime.start_scope = AsyncMock(return_value=True)
    return runtime


# ---------------------------------------------------------------------------
# Desired-state round trip
# ---------------------------------------------------------------------------


class TestDesiredStateRoundTrip:

    @pytest.mark.asyncio
    async def test_stored_live_scope_is_listed(self, db: AsyncSession) -> None:
        await store_desired_state(
            db,
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            desired_state=DesiredScopeState.LIVE,
        )
        await db.commit()

        assert await fetch_live_scopes(db) == [(_TENANT, _GH)]

    @pytest.mark.asyncio
    async def test_paused_scope_is_not_listed(self, db: AsyncSession) -> None:
        await store_desired_state(
            db,
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            desired_state=DesiredScopeState.PAUSED,
        )
        await db.commit()

        assert await fetch_live_scopes(db) == []
        assert (
            await fetch_desired_state(db, tenant_id=_TENANT, greenhouse_id=_GH)
            is DesiredScopeState.PAUSED
        )

    @pytest.mark.asyncio
    async def test_restoring_state_updates_in_place(self, db: AsyncSession) -> None:
        """Go Live then pause leaves one row saying paused, not two rows."""
        for state in (DesiredScopeState.LIVE, DesiredScopeState.PAUSED):
            await store_desired_state(
                db, tenant_id=_TENANT, greenhouse_id=_GH, desired_state=state
            )
        await db.commit()

        rows = (await db.execute(select(LiveScopeState))).scalars().all()
        assert len(rows) == 1
        assert rows[0].desired_state == "paused"

    @pytest.mark.asyncio
    async def test_unset_scope_has_no_desired_state(self, db: AsyncSession) -> None:
        assert (
            await fetch_desired_state(db, tenant_id=_TENANT, greenhouse_id=_OTHER_GH)
            is None
        )


class TestMissingTableIsTolerated:
    """A skewed deploy must degrade to pre-028 behaviour, never crash."""

    @pytest.mark.asyncio
    async def test_fetch_returns_empty(self, unmigrated_db: AsyncSession) -> None:
        assert await fetch_live_scopes(unmigrated_db) == []

    @pytest.mark.asyncio
    async def test_store_reports_failure_without_raising(
        self, unmigrated_db: AsyncSession
    ) -> None:
        persisted = await store_desired_state(
            unmigrated_db,
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            desired_state=DesiredScopeState.LIVE,
        )
        assert persisted is False

    @pytest.mark.asyncio
    async def test_session_stays_usable_after_a_failed_store(
        self, unmigrated_db: AsyncSession
    ) -> None:
        """The rollback inside store_desired_state must leave the session
        able to run the caller's own statements — a wipe relies on this."""
        await store_desired_state(
            unmigrated_db,
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            desired_state=DesiredScopeState.PAUSED,
        )
        result = await unmigrated_db.execute(select(SensorReading.id))
        assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Boot resume
# ---------------------------------------------------------------------------


class TestBootResume:

    @pytest_asyncio.fixture(autouse=True)
    async def _use_test_db(
        self, monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker
    ) -> None:
        monkeypatch.setattr(live_boot_resume, "AsyncSessionLocal", session_factory)

    @pytest.mark.asyncio
    async def test_stored_live_scope_is_started(
        self, db: AsyncSession, session_factory: async_sessionmaker
    ) -> None:
        async with session_factory() as writer:
            await store_desired_state(
                writer,
                tenant_id=_TENANT,
                greenhouse_id=_GH,
                desired_state=DesiredScopeState.LIVE,
            )
            await writer.commit()

        runtime = _fake_runtime([(_TENANT, _GH), (_TENANT, _OTHER_GH)])
        summary = await resume_persisted_live_scopes(runtime)

        runtime.start_scope.assert_awaited_once_with(_TENANT, _GH)
        assert summary.resumed == ((_TENANT, _GH),)
        assert summary.idle == ((_TENANT, _OTHER_GH),)
        assert summary.stored_live_not_configured == ()
        assert summary.failed == ()

    @pytest.mark.asyncio
    async def test_nothing_starts_when_no_state_is_stored(self) -> None:
        """LIVE_AUTOSTART_ENABLED=false semantics: a scope nobody turned on
        must not have connectors running after a restart."""
        runtime = _fake_runtime([(_TENANT, _GH)])

        summary = await resume_persisted_live_scopes(runtime)

        runtime.start_scope.assert_not_awaited()
        assert summary.resumed == ()
        assert summary.idle == ((_TENANT, _GH),)

    @pytest.mark.asyncio
    async def test_stored_live_but_unconfigured_scope_is_named_not_started(
        self, session_factory: async_sessionmaker
    ) -> None:
        """A scope dropped from MOCK_SCADA_SCOPES must not resume, and must
        be reported — a silently dark feed is the bug being fixed."""
        async with session_factory() as writer:
            await store_desired_state(
                writer,
                tenant_id=_TENANT,
                greenhouse_id="gh_removed_from_env",
                desired_state=DesiredScopeState.LIVE,
            )
            await writer.commit()

        runtime = _fake_runtime([(_TENANT, _GH)])
        summary = await resume_persisted_live_scopes(runtime)

        runtime.start_scope.assert_not_awaited()
        assert summary.resumed == ()
        assert summary.stored_live_not_configured == (
            (_TENANT, "gh_removed_from_env"),
        )

    @pytest.mark.asyncio
    async def test_one_failing_scope_does_not_dark_the_others(
        self, session_factory: async_sessionmaker
    ) -> None:
        async with session_factory() as writer:
            for greenhouse in (_GH, _OTHER_GH):
                await store_desired_state(
                    writer,
                    tenant_id=_TENANT,
                    greenhouse_id=greenhouse,
                    desired_state=DesiredScopeState.LIVE,
                )
            await writer.commit()

        runtime = _fake_runtime([(_TENANT, _GH), (_TENANT, _OTHER_GH)])

        async def start(tenant_id: str, greenhouse_id: str) -> bool:
            if greenhouse_id == _GH:
                raise RuntimeError("connector wedged")
            return True

        runtime.start_scope = AsyncMock(side_effect=start)

        summary = await resume_persisted_live_scopes(runtime)

        assert summary.failed == ((_TENANT, _GH),)
        assert summary.resumed == ((_TENANT, _OTHER_GH),)

    @pytest.mark.asyncio
    async def test_boot_resume_is_a_noop_on_an_unmigrated_db(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The API must boot against an older schema, not crash."""
        engine = create_async_engine("sqlite+aiosqlite://")
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        monkeypatch.setattr(live_boot_resume, "AsyncSessionLocal", factory)
        runtime = _fake_runtime([(_TENANT, _GH)])

        summary = await resume_persisted_live_scopes(runtime)

        assert summary.resumed == ()
        assert summary.idle == ((_TENANT, _GH),)
        runtime.start_scope.assert_not_awaited()
        await engine.dispose()


class TestBootLogLine:

    def test_log_names_every_bucket(self, caplog: pytest.LogCaptureFixture) -> None:
        summary = live_boot_resume.BootResumeSummary(
            resumed=((_TENANT, _GH),),
            idle=((_TENANT, _OTHER_GH),),
            stored_live_not_configured=(("tenant_gone", "gh_X"),),
        )
        with caplog.at_level(logging.INFO, logger=live_boot_resume.logger.name):
            log_boot_resume(summary)

        startup = next(
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO
        )
        assert "Live runtime started" in startup
        assert f"resumed: {_TENANT}/{_GH}" in startup
        assert f"idle: {_TENANT}/{_OTHER_GH}" in startup
        assert "stored live but not configured: tenant_gone/gh_X" in startup

    def test_empty_buckets_render_as_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger=live_boot_resume.logger.name):
            log_boot_resume(live_boot_resume.BootResumeSummary())

        assert "resumed: (none)" in caplog.text

    def test_unconfigured_stored_live_scope_also_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        summary = live_boot_resume.BootResumeSummary(
            stored_live_not_configured=(("tenant_gone", "gh_X"),),
        )
        with caplog.at_level(logging.INFO, logger=live_boot_resume.logger.name):
            log_boot_resume(summary)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "tenant_gone/gh_X" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# A wipe wins over a prior Go Live
# ---------------------------------------------------------------------------


class TestWipeWins:

    @pytest.mark.asyncio
    async def test_wipe_overwrites_stored_live_with_paused(
        self, db: AsyncSession
    ) -> None:
        await store_desired_state(
            db,
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            desired_state=DesiredScopeState.LIVE,
        )
        await db.commit()

        await wipe_tenant_data(db, None, _TENANT, _GH)

        assert (
            await fetch_desired_state(db, tenant_id=_TENANT, greenhouse_id=_GH)
            is DesiredScopeState.PAUSED
        )
        assert await fetch_live_scopes(db) == []

    @pytest.mark.asyncio
    async def test_wiped_scope_is_not_resumed_on_the_next_boot(
        self,
        db: AsyncSession,
        session_factory: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await store_desired_state(
            db,
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            desired_state=DesiredScopeState.LIVE,
        )
        await db.commit()
        await wipe_tenant_data(db, None, _TENANT, _GH)

        monkeypatch.setattr(live_boot_resume, "AsyncSessionLocal", session_factory)
        runtime = _fake_runtime([(_TENANT, _GH)])
        summary = await resume_persisted_live_scopes(runtime)

        runtime.start_scope.assert_not_awaited()
        assert summary.idle == ((_TENANT, _GH),)


# ---------------------------------------------------------------------------
# The whole point: a resumed scope writes rows
# ---------------------------------------------------------------------------


def _mock_scope_config(poll_interval: float) -> SourceConfig:
    """One mock source carrying all four canonical channels.

    All four in a single source so every merged bucket is complete and
    reaches ``sensor_readings`` too, not just ``raw_readings``.
    """
    return SourceConfig(
        source_id=f"{_TENANT}_{_GH}_mock_all",
        vendor="mock",
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        channels=["temperature", "humidity", "co2", "light"],
        priority=10,
        poll_interval_seconds=poll_interval,
        params={"seed": 7, "demo_mode": False},
    )


class TestResumedScopeWritesRows:
    """Prove persistence, not just state. Production has zero live rows."""

    @pytest_asyncio.fixture
    async def live_stack(
        self,
        session_factory: async_sessionmaker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> AsyncGenerator[tuple[LiveRuntime, async_sessionmaker], None]:
        monkeypatch.setattr(live_boot_resume, "AsyncSessionLocal", session_factory)
        redis = FakeRedis()
        hub = LiveHub(redis_client=redis, block_ms=50)
        batcher = PersistenceBatcher(
            flush_interval=0.05, session_factory=session_factory
        )
        # The real scope builder, so on_reading runs the production chain
        # (suppression gate -> derived channels -> scorer -> hub -> batcher)
        # rather than a hand-rolled stand-in that could skip a step.
        scope = _build_scope(
            hub, _TENANT, _GH, [MockScadaSource(_mock_scope_config(0.02))], batcher
        )
        runtime = LiveRuntime(hub, [scope], batcher)
        await runtime.start()
        try:
            yield runtime, session_factory
        finally:
            await runtime.stop()
            close_fn = getattr(redis, "aclose", None) or redis.close
            await close_fn()

    @staticmethod
    async def _await_persisted_sources(
        factory: async_sessionmaker, model: type, timeout: float = 20.0
    ) -> list[str]:
        """Poll until the batcher has flushed rows, returning their ``source``.

        The source values are read in the *same* query as the existence
        check on purpose. An earlier version counted rows in one poll and
        read sources in a second, and failed once when a flush landed
        between the two — the count is only meaningful alongside the values
        it describes.

        ``sensor_readings`` needs a bucket carrying all four canonical
        channels (incomplete buckets reach ``raw_readings`` only), so the
        timeout is generous.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            async with factory() as session:
                sources = (
                    await session.execute(
                        select(model.source).where(
                            model.tenant_id == _TENANT,
                            model.greenhouse_id == _GH,
                        )
                    )
                ).scalars().all()
            if sources:
                return list(sources)
            if asyncio.get_running_loop().time() >= deadline:
                return []
            await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_idle_scope_writes_nothing(
        self, live_stack: tuple[LiveRuntime, async_sessionmaker]
    ) -> None:
        """No stored state -> boot resume starts nothing -> no rows at all."""
        runtime, factory = live_stack

        summary = await resume_persisted_live_scopes(runtime)
        assert summary.resumed == ()
        await asyncio.sleep(0.4)

        assert not runtime.is_scope_active(_TENANT, _GH)
        async with factory() as session:
            raw = (await session.execute(select(RawReading))).scalars().all()
        assert raw == []

    @pytest.mark.asyncio
    async def test_boot_resumed_scope_persists_to_both_tables(
        self, live_stack: tuple[LiveRuntime, async_sessionmaker]
    ) -> None:
        runtime, factory = live_stack
        async with factory() as writer:
            await store_desired_state(
                writer,
                tenant_id=_TENANT,
                greenhouse_id=_GH,
                desired_state=DesiredScopeState.LIVE,
            )
            await writer.commit()

        summary = await resume_persisted_live_scopes(runtime)
        assert summary.resumed == ((_TENANT, _GH),)
        assert runtime.is_scope_active(_TENANT, _GH)

        raw_sources = await self._await_persisted_sources(factory, RawReading)
        assert raw_sources, "resumed scope wrote no raw_readings row"
        assert set(raw_sources) == {"live"}

        sensor_sources = await self._await_persisted_sources(factory, SensorReading)
        assert sensor_sources, "resumed scope wrote no sensor_readings row"
        assert set(sensor_sources) == {"live"}


# ---------------------------------------------------------------------------
# Staleness signal on GET /api/live/status
# ---------------------------------------------------------------------------


def _reading(ts: datetime.datetime) -> SensorReading:
    return SensorReading(
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        timestamp=ts,
        temperature=22.0,
        humidity=60.0,
        co2=400.0,
        light=500.0,
        source="live",
    )


class TestScopeFreshness:

    @staticmethod
    def _runtime(*, configured: bool = True, active: bool = True) -> MagicMock:
        runtime = MagicMock()
        runtime.is_scope_configured.return_value = configured
        runtime.is_scope_active.return_value = active
        return runtime

    @pytest.mark.asyncio
    async def test_live_scope_with_a_recent_row_is_fresh(
        self, db: AsyncSession
    ) -> None:
        now = datetime.datetime(2026, 10, 16, 12, 0, tzinfo=datetime.timezone.utc)
        db.add(_reading(now - datetime.timedelta(seconds=30)))
        await db.commit()

        freshness = await live_freshness.build_scope_freshness(
            db,
            runtime=self._runtime(),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            now=now,
        )

        assert freshness.state == "live"
        assert freshness.age_seconds == pytest.approx(30.0)
        assert freshness.is_stale is False

    @pytest.mark.asyncio
    async def test_live_scope_past_the_threshold_is_stale(
        self, db: AsyncSession
    ) -> None:
        now = datetime.datetime(2026, 10, 16, 12, 0, tzinfo=datetime.timezone.utc)
        age = live_freshness.LIVE_STALE_AFTER_SECONDS + 60
        db.add(_reading(now - datetime.timedelta(seconds=age)))
        await db.commit()

        freshness = await live_freshness.build_scope_freshness(
            db,
            runtime=self._runtime(),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            now=now,
        )

        assert freshness.is_stale is True
        assert freshness.age_seconds == pytest.approx(age)

    @pytest.mark.asyncio
    async def test_live_scope_that_never_wrote_a_row_is_stale(
        self, db: AsyncSession
    ) -> None:
        """The exact production state: Go Live on, nothing ever persisted."""
        freshness = await live_freshness.build_scope_freshness(
            db, runtime=self._runtime(), tenant_id=_TENANT, greenhouse_id=_GH
        )

        assert freshness.newest_reading_at is None
        assert freshness.age_seconds is None
        assert freshness.is_stale is True

    @pytest.mark.asyncio
    async def test_paused_scope_is_never_stale(self, db: AsyncSession) -> None:
        now = datetime.datetime(2026, 10, 16, 12, 0, tzinfo=datetime.timezone.utc)
        db.add(_reading(now - datetime.timedelta(days=30)))
        await db.commit()

        freshness = await live_freshness.build_scope_freshness(
            db,
            runtime=self._runtime(active=False),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            now=now,
        )

        assert freshness.state == "paused"
        assert freshness.is_stale is False

    @pytest.mark.asyncio
    async def test_unconfigured_scope_reports_not_configured(
        self, db: AsyncSession
    ) -> None:
        freshness = await live_freshness.build_scope_freshness(
            db,
            runtime=self._runtime(configured=False),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
        )

        assert freshness.state == "not_configured"
        assert freshness.is_stale is False

    @pytest.mark.asyncio
    async def test_future_dated_reading_gives_a_negative_age_and_is_not_stale(
        self, db: AsyncSession
    ) -> None:
        """Readings here run ahead of wall clock; the age is NOT clamped."""
        now = datetime.datetime(2026, 10, 16, 12, 0, tzinfo=datetime.timezone.utc)
        db.add(_reading(now + datetime.timedelta(days=60)))
        await db.commit()

        freshness = await live_freshness.build_scope_freshness(
            db,
            runtime=self._runtime(),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            now=now,
        )

        assert freshness.age_seconds is not None
        assert freshness.age_seconds < 0
        assert freshness.is_stale is False

    @pytest.mark.asyncio
    async def test_raw_only_rows_still_count_as_fresh(self, db: AsyncSession) -> None:
        """An incomplete bucket reaches raw_readings only; that is a working
        feed and must not be reported as stale."""
        now = datetime.datetime(2026, 10, 16, 12, 0, tzinfo=datetime.timezone.utc)
        db.add(
            RawReading(
                tenant_id=_TENANT,
                greenhouse_id=_GH,
                timestamp=now - datetime.timedelta(seconds=5),
                source="live",
                data={"temperature": 22.0},
            )
        )
        await db.commit()

        freshness = await live_freshness.build_scope_freshness(
            db,
            runtime=self._runtime(),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            now=now,
        )

        assert freshness.age_seconds == pytest.approx(5.0)
        assert freshness.is_stale is False


class TestStatusEndpointShape:

    @pytest_asyncio.fixture
    async def async_client(
        self, db: AsyncSession
    ) -> AsyncGenerator[AsyncClient, None]:
        app.dependency_overrides[get_db] = lambda: db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
        app.dependency_overrides.pop(get_db, None)

    @pytest.mark.asyncio
    async def test_status_keeps_sources_and_adds_freshness(
        self, async_client: AsyncClient
    ) -> None:
        runtime = MagicMock()
        runtime.source_statuses.return_value = []
        runtime.is_scope_configured.return_value = False
        runtime.is_scope_active.return_value = False
        app.state.live_runtime = runtime

        resp = await async_client.get(
            "/api/live/status",
            params={"tenant_id": _TENANT, "greenhouse_id": _GH},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert "sources" in body
        assert "greenhouse_status" in body
        assert body["freshness"] == {
            "state": "not_configured",
            "newest_reading_at": None,
            "age_seconds": None,
            "is_stale": False,
            "threshold_seconds": live_freshness.LIVE_STALE_AFTER_SECONDS,
        }

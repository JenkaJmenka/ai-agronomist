"""Harness that runs the **real** ``src.api.main.lifespan`` against a test DB.

Why not call ``resume_persisted_live_scopes`` directly
------------------------------------------------------
The defect migration 028 fixes is a *boot-path* defect: every redeploy
returned every scope to idle.  A test that invokes the resume service
directly proves the service works and says nothing about whether boot calls
it, with the right runtime, inside the right ``try``.  So these acceptance
cases drive ``lifespan(app)`` itself — the same async context manager
uvicorn enters — and only external I/O is substituted:

* ``init_db`` / ``engine`` / ``AsyncSessionLocal`` -> a per-test SQLite file.
  ``init_db`` in particular **must** be stubbed: left real it would
  ``create_all`` against the session-wide ``./test_global.db`` and silently
  flip ``desired_state_persisted`` for every other API test that touches it.
* Redis (cache client + live hub) -> ``fakeredis``.
* ``close_mq`` -> no-op.

Everything else is production code: ``LiveRuntime.build_from_config`` really
parses ``MOCK_SCADA_SCOPES``, real mock connectors tick, a real
``PersistenceBatcher`` writes rows, and the real boot-resume + log line run.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, AsyncIterator, Optional, Sequence

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api import main as api_main
from src.api.services import live_boot_resume
from src.db.models import Base, RawReading, SensorReading
from src.live.hub import LiveHub
from src.live.persistence import PersistenceBatcher

TENANT = "boot_tenant"
GREENHOUSE = "gh_boot"
SCOPE = f"{TENANT}:{GREENHOUSE}"

# A scope the test tenant is NOT part of — used to simulate an operator
# dropping TENANT/GREENHOUSE out of MOCK_SCADA_SCOPES between restarts.
OTHER_SCOPE = "other_tenant:gh_other"

# Sub-second cadence so a boot writes rows within a test's patience.
# Mock sources poll at 1x/1.5x/2x/2.5x of the interval, so the merge grace
# must exceed the slowest of them or its buckets flush before its readings
# land (same reasoning as tests/live/test_live_gating_persistence.py).
MOCK_INTERVAL_SECONDS = 0.05
MERGE_GRACE_SECONDS = 0.15
FLUSH_INTERVAL_SECONDS = 0.1

# Long enough for ~30 connector ticks and ~15 flushes: an idle scope that
# leaks even one row gets many chances to prove it.
IDLE_WATCH_SECONDS = 1.5

# Upper bound on "rows should appear now that the scope is live".
ROW_TIMEOUT_SECONDS = 20.0


async def _anoop(*_args: Any, **_kwargs: Any) -> None:
    """Async stand-in for a startup/shutdown side effect we do not want."""
    return None


class _FakeRedisModule:
    """Stands in for ``redis.asyncio`` inside ``src.api.main`` only."""

    @staticmethod
    def from_url(*_args: Any, **_kwargs: Any) -> FakeRedis:
        return FakeRedis(decode_responses=True)


class _FailingScoringModule:
    """Makes ``from src.live.scoring import LiveScorer`` raise ImportError.

    Scoring sits downstream of everything under test and would drag a
    ``torch.load`` of the tenant checkpoint into every boot.
    ``build_scopes_from_config`` already degrades to an unscored pipeline.
    """

    def __getattr__(self, name: str) -> Any:
        raise ImportError(f"{name} disabled for boot-acceptance tests")


class LiveBootHarness:
    """Boots the real lifespan repeatedly against one test database."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory
        self._hub: Optional[LiveHub] = None
        self._redis_clients: list[FakeRedis] = []

    def _get_live_hub(self) -> LiveHub:
        """Memoized per boot, so lifespan teardown closes the hub it opened."""
        if self._hub is None:
            redis = FakeRedis()
            self._redis_clients.append(redis)
            self._hub = LiveHub(redis_client=redis, block_ms=10)
        return self._hub

    @asynccontextmanager
    async def boot(self) -> AsyncIterator[Any]:
        """Run one full API startup/shutdown cycle; yield the live runtime."""
        self._hub = None
        async with api_main.lifespan(api_main.app):
            yield getattr(api_main.app.state, "live_runtime", None)

    async def aclose(self) -> None:
        for redis in self._redis_clients:
            close_fn = getattr(redis, "aclose", None) or redis.close
            await close_fn()
        self._redis_clients.clear()

    async def clear_reading_rows(self) -> None:
        """Drop every reading row so the next boot starts from a clean slate.

        Needed between boots because the pre-restart boot also streams: a
        bare row count afterwards could not tell "written after the restart"
        from "left over from before it".
        """
        async with self.session_factory() as session:
            await session.execute(delete(RawReading))
            await session.execute(delete(SensorReading))
            await session.commit()

    async def reading_sources(self, model: type) -> list[str]:
        """Every ``source`` value stored for the test scope in *model*."""
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(model.source).where(
                        model.tenant_id == TENANT,
                        model.greenhouse_id == GREENHOUSE,
                    )
                )
            ).scalars().all()
        return list(rows)

    async def await_reading_sources(
        self, model: type, timeout: float = ROW_TIMEOUT_SECONDS
    ) -> list[str]:
        """Poll until rows exist, returning their ``source`` values.

        Existence and the values are read in a **single** query on purpose.
        An earlier backend revision counted rows in one poll and read sources
        in a second, and flaked when a flush landed between them — a count is
        only meaningful alongside the values it describes.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            sources = await self.reading_sources(model)
            if sources:
                return sources
            if asyncio.get_running_loop().time() >= deadline:
                return []
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)


async def make_session_factory(
    db_path: str, *, include_scope_state: bool = True
) -> tuple[async_sessionmaker, Any]:
    """Build a temp-file SQLite DB with the production ORM schema.

    ``include_scope_state=False`` reproduces the deploy-skew hazard: only the
    API service runs ``alembic upgrade head`` (``docker/start-api.sh``), so
    newer code can meet a schema without ``live_scope_state``.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if include_scope_state or table.name != "live_scope_state"
    ]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return factory, engine


def install_boot_patches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_factory: async_sessionmaker,
    engine: Any,
    scopes: Sequence[str] = (SCOPE,),
) -> LiveBootHarness:
    """Point the lifespan's external dependencies at test doubles."""
    harness = LiveBootHarness(session_factory)

    # --- startup/shutdown I/O the test must not perform for real ---------
    monkeypatch.setattr(api_main, "init_db", _anoop)
    monkeypatch.setattr(api_main, "close_mq", _anoop)
    monkeypatch.setattr(api_main, "aioredis", _FakeRedisModule())
    monkeypatch.setattr(api_main, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(api_main, "engine", engine)
    monkeypatch.setattr(live_boot_resume, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(
        "src.api.services.live_hub_provider.get_live_hub", harness._get_live_hub
    )
    monkeypatch.setitem(sys.modules, "src.live.scoring", _FailingScoringModule())

    set_configured_scopes(monkeypatch, scopes)

    # Shipped default, restated because the bug was that it did not hold.
    monkeypatch.setattr("src.live.runtime.LIVE_AUTOSTART_ENABLED", False)
    monkeypatch.setattr("src.live.runtime_builder.LIVE_PERSIST_ENABLED", True)
    monkeypatch.setattr("src.live.runtime_builder.LIVE_SOURCES_JSON", "")
    monkeypatch.setattr(
        "src.live.runtime_builder.LIVE_MERGE_GRACE_SECONDS", MERGE_GRACE_SECONDS
    )
    # The flush interval is bound as a function default at import time and so
    # cannot be reached through src.config — substitute the factory instead.
    monkeypatch.setattr(
        "src.live.runtime_builder.PersistenceBatcher",
        lambda: PersistenceBatcher(
            flush_interval=FLUSH_INTERVAL_SECONDS, session_factory=session_factory
        ),
    )
    # The builder stamps poll intervals onto SourceConfig at construction
    # time, so mock_factory's copy of the constant is the one that matters.
    for module_path in (
        "src.config.MOCK_SCADA_INTERVAL_SECONDS",
        "src.live.runtime_builder.MOCK_SCADA_INTERVAL_SECONDS",
        "src.live.sources.mock_factory.MOCK_SCADA_INTERVAL_SECONDS",
    ):
        monkeypatch.setattr(module_path, MOCK_INTERVAL_SECONDS)

    return harness


def set_configured_scopes(
    monkeypatch: pytest.MonkeyPatch, scopes: Sequence[str]
) -> None:
    """Rewrite MOCK_SCADA_SCOPES as the next boot's build step will read it."""
    monkeypatch.setattr("src.live.runtime_builder.MOCK_SCADA_ENABLED", True)
    monkeypatch.setattr("src.live.runtime_builder.MOCK_SCADA_SCOPES", ",".join(scopes))


@asynccontextmanager
async def booted_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    *,
    include_scope_state: bool = True,
    scopes: Sequence[str] = (SCOPE,),
) -> AsyncGenerator[LiveBootHarness, None]:
    """Full harness lifecycle: schema, patches, teardown."""
    factory, engine = await make_session_factory(
        f"{tmp_path}/live_boot.db", include_scope_state=include_scope_state
    )
    harness = install_boot_patches(
        monkeypatch, session_factory=factory, engine=engine, scopes=scopes
    )
    try:
        yield harness
    finally:
        await harness.aclose()
        if hasattr(api_main.app.state, "live_runtime"):
            delattr(api_main.app.state, "live_runtime")
        await engine.dispose()

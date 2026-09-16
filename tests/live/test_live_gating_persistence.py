"""Data-layer proof that live data reaches the DB only after Go Live.

Operator bug this pins down: *"when the live streaming is off, changing the
date frames generates data that is not from the originally uploaded CSV
file."*  Mock SCADA connectors used to stream from API boot regardless of the
UI toggle, so ``PersistenceBatcher`` wrote synthetic ``source="live"`` rows
into ``sensor_readings`` + ``raw_readings`` and a widened date range merged
fabricated rows into the operator's uploaded CSV.

The existing runtime tests assert at the *task* level (tasks spawned /
cancelled).  These assert at the *data* level: a real ``LiveRuntime`` built
from config, real mock connectors, a real ``PersistenceBatcher``, a real
temp-file SQLite database and fakeredis — then count rows and read the window
back through ``fetch_merged_readings``.

Why raw_readings carries the "live data arrived" assertions
------------------------------------------------------------
``SensorReading`` rows are only written for buckets holding all four
canonical channels, and the mock scope splits those channels across sources
with different poll intervals (climate 1x, co2 1.5x).  At the sub-second
intervals used here whether a bucket is complete is a timing coincidence, so
canonical rows are not a deterministic signal.  ``RawReading`` is written for
every non-empty bucket, so it is the deterministic one.  The *idle*
assertions still cover both tables — zero rows means zero rows.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base, RawReading, SensorReading
from src.live.hub import LiveHub
from src.live.persistence import PersistenceBatcher
from src.live.runtime import LiveRuntime
from src.live.scope_control import clear_all_suppressed_scopes
from tests.live.conftest import _safe_redis_close

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT = "gate_tenant"
_GH = "gate_gh"
_SCOPE = f"{_TENANT}:{_GH}"

# Sub-second timings so the whole module runs in a few seconds. The mock
# sources poll at 1x/1.5x/2x/2.5x this value; the merge grace must exceed the
# slowest (2.5x) or its buckets are flushed before its readings land.
_MOCK_INTERVAL_SECONDS = 0.05
_MERGE_GRACE_SECONDS = 0.15
_FLUSH_INTERVAL_SECONDS = 0.1

# Long enough to cover ~30 connector ticks and ~15 persistence flushes, so a
# silent scope that leaks even one row has many chances to prove it.
_IDLE_WATCH_SECONDS = 1.5

# Upper bound for "rows should show up once live" polling.
_LIVE_ROW_TIMEOUT_SECONDS = 5.0

# The operator's "widen the date frame" gesture, taken to its extreme.
_WIDE_START = datetime(2000, 1, 1, tzinfo=timezone.utc)
_WIDE_END = datetime(2100, 1, 1, tzinfo=timezone.utc)

# Seeded stand-in for an uploaded CSV.
_CSV_SOURCE = "csv_upload"
_CSV_FIRST_TS = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)
_CSV_ROW_COUNT = 5


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session_factory(tmp_path) -> AsyncGenerator[async_sessionmaker, None]:
    """Real temp-file SQLite DB with the production schema."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/live_gating.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    client = FakeRedis()
    yield client
    await _safe_redis_close(client)


@pytest.fixture
def hub(fake_redis: FakeRedis) -> LiveHub:
    return LiveHub(redis_client=fake_redis, block_ms=10)


@pytest.fixture(autouse=True)
def _reset_scope_gate() -> None:
    """No suppressed-scope state may leak in or out of a case."""
    clear_all_suppressed_scopes()
    yield
    clear_all_suppressed_scopes()


@pytest.fixture(autouse=True)
def _live_config(monkeypatch: pytest.MonkeyPatch, session_factory) -> None:
    """Mock SCADA on, persistence on, autostart off — the shipped defaults.

    ``MOCK_SCADA_INTERVAL_SECONDS`` is patched in all three namespaces that
    read it (same as ``test_runtime_smoke``): the builder derives the merge
    bucket width from ``SourceConfig.poll_interval_seconds``, which
    ``mock_factory`` stamps at construction time.
    """
    monkeypatch.setattr("src.live.runtime_builder.MOCK_SCADA_ENABLED", True)
    monkeypatch.setattr("src.live.runtime_builder.MOCK_SCADA_SCOPES", _SCOPE)
    monkeypatch.setattr("src.live.runtime_builder.LIVE_SOURCES_JSON", "")
    monkeypatch.setattr("src.live.runtime_builder.LIVE_PERSIST_ENABLED", True)
    monkeypatch.setattr(
        "src.live.runtime_builder.MOCK_SCADA_INTERVAL_SECONDS", _MOCK_INTERVAL_SECONDS
    )
    monkeypatch.setattr("src.config.MOCK_SCADA_INTERVAL_SECONDS", _MOCK_INTERVAL_SECONDS)
    monkeypatch.setattr(
        "src.live.sources.mock_factory.MOCK_SCADA_INTERVAL_SECONDS", _MOCK_INTERVAL_SECONDS
    )
    monkeypatch.setattr(
        "src.live.runtime_builder.LIVE_MERGE_GRACE_SECONDS", _MERGE_GRACE_SECONDS
    )
    # The default flush interval is bound as a function default at import time,
    # so it cannot be monkeypatched through src.config. Substitute a factory
    # that builds the real batcher with a test-sized interval against the temp DB.
    monkeypatch.setattr(
        "src.live.runtime_builder.PersistenceBatcher",
        lambda: PersistenceBatcher(
            flush_interval=_FLUSH_INTERVAL_SECONDS, session_factory=session_factory
        ),
    )
    # Shipped default — restated because the bug was that it did not hold.
    monkeypatch.setattr("src.live.runtime.LIVE_AUTOSTART_ENABLED", False)


@pytest.fixture(autouse=True)
def _passthrough_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Break the lazy ``LiveScorer`` import so the runtime scores nothing.

    Scoring sits downstream of the gate under test and would drag in the
    global session factory plus a ``torch.load`` on the tenant's checkpoint,
    making these tests slow and nondeterministic for no added coverage.
    ``build_scopes_from_config`` already handles the ImportError by running
    the pipeline unscored.
    """
    sys.modules.pop("src.live.scoring", None)

    class _FailImport:
        def __getattr__(self, name: str) -> Any:
            raise ImportError("LiveScorer disabled for gating tests")

    sys.modules["src.live.scoring"] = _FailImport()  # type: ignore[assignment]
    yield
    sys.modules.pop("src.live.scoring", None)


@pytest_asyncio.fixture
async def runtime(hub: LiveHub) -> AsyncGenerator[LiveRuntime, None]:
    """Started runtime; always stopped so no asyncio task outlives the case."""
    rt = LiveRuntime.build_from_config(hub)
    await rt.start()
    yield rt
    await rt.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _count(session_factory, model) -> int:
    """Row count for the test scope in *model*'s table."""
    async with session_factory() as session:
        result = await session.execute(
            select(func.count(model.id)).where(
                model.tenant_id == _TENANT, model.greenhouse_id == _GH
            )
        )
        return result.scalar() or 0


async def _distinct_sources(session_factory, model) -> set[str | None]:
    """Distinct ``source`` values present for the test scope."""
    async with session_factory() as session:
        result = await session.execute(
            select(model.source).where(
                model.tenant_id == _TENANT, model.greenhouse_id == _GH
            )
        )
        return set(result.scalars().all())


async def _wait_for_raw_rows(session_factory, *, more_than: int) -> int:
    """Poll until raw_readings exceeds *more_than* rows, or time out."""
    deadline = asyncio.get_event_loop().time() + _LIVE_ROW_TIMEOUT_SECONDS
    count = await _count(session_factory, RawReading)
    while count <= more_than and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
        count = await _count(session_factory, RawReading)
    return count


def _csv_timestamps() -> list[datetime]:
    return [_CSV_FIRST_TS + timedelta(minutes=i) for i in range(_CSV_ROW_COUNT)]


def _as_utc(iso_value: str) -> datetime:
    """Parse a timestamp returned by the read path as an aware UTC datetime.

    SQLite hands back naive datetimes so the ISO string carries no offset,
    while Postgres keeps ``+00:00``. Both mean UTC; normalise before compare.
    """
    parsed = datetime.fromisoformat(iso_value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _seed_uploaded_csv(session_factory) -> None:
    """Insert the rows an operator's CSV upload would have produced."""
    async with session_factory() as session:
        for index, ts in enumerate(_csv_timestamps()):
            channels = {
                "temperature": 21.0 + index,
                "humidity": 60.0 + index,
                "co2": 400.0 + index,
                "light": 500.0 + index,
            }
            session.add(
                RawReading(
                    tenant_id=_TENANT,
                    greenhouse_id=_GH,
                    timestamp=ts,
                    source=_CSV_SOURCE,
                    data=channels,
                )
            )
            session.add(
                SensorReading(
                    tenant_id=_TENANT,
                    greenhouse_id=_GH,
                    timestamp=ts,
                    anomaly_score=0.1,
                    source=None,  # CSV uploads leave the provenance tag unset
                    **channels,
                )
            )
        await session.commit()


# ---------------------------------------------------------------------------
# 1 + 2 — live off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_runtime_writes_no_rows(runtime: LiveRuntime, session_factory) -> None:
    """A runtime started with autostart off persists nothing, ever.

    The operator's exact complaint: no Go Live was pressed, so neither
    sensor_readings nor raw_readings may gain a single row no matter how long
    the process runs.
    """
    assert not runtime.is_scope_active(_TENANT, _GH)

    await asyncio.sleep(_IDLE_WATCH_SECONDS)

    assert await _count(session_factory, SensorReading) == 0
    assert await _count(session_factory, RawReading) == 0


@pytest.mark.asyncio
async def test_idle_runtime_leaves_uploaded_csv_window_untouched(
    runtime: LiveRuntime, session_factory
) -> None:
    """Widening the date frame with live off returns only the uploaded CSV rows."""
    await _seed_uploaded_csv(session_factory)

    await asyncio.sleep(_IDLE_WATCH_SECONDS)

    async with session_factory() as session:
        from src.api.services.readings_service import fetch_merged_readings

        rows = await fetch_merged_readings(
            session,
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            start=_WIDE_START,
            end=_WIDE_END,
        )

    assert [_as_utc(row["timestamp"]) for row in rows] == _csv_timestamps()
    assert [row["temperature"] for row in rows] == [21.0 + i for i in range(_CSV_ROW_COUNT)]
    assert {row["source"] for row in rows} == {_CSV_SOURCE}


# ---------------------------------------------------------------------------
# 3 + 4 + 5 — Go Live, turn off, go live again
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_rows_appear_stop_freezes_them_and_restart_resumes(
    runtime: LiveRuntime, session_factory
) -> None:
    """Go Live writes rows, turning live off freezes the table, Go Live resumes."""
    # --- 3. Go Live: rows tagged source="live" start arriving.
    assert await runtime.start_scope(_TENANT, _GH) is True
    after_go_live = await _wait_for_raw_rows(session_factory, more_than=0)
    assert after_go_live > 0, "no raw_readings rows after Go Live"
    assert await _distinct_sources(session_factory, RawReading) == {"live"}

    # --- 4. Turn live off: the row count stops moving.
    assert await runtime.stop_scope(_TENANT, _GH) is True
    assert not runtime.is_scope_active(_TENANT, _GH)
    # Two flush intervals of settling for any flush already in flight when the
    # scope's tasks were cancelled; the frozen count is measured after it.
    await asyncio.sleep(_FLUSH_INTERVAL_SECONDS * 2)
    frozen_count = await _count(session_factory, RawReading)

    await asyncio.sleep(_IDLE_WATCH_SECONDS)
    assert await _count(session_factory, RawReading) == frozen_count

    # --- 5. Go Live again: rows resume (supervised() would have restarted a
    # cleanly-exited coroutine, so stop_scope must have truly cancelled).
    assert await runtime.start_scope(_TENANT, _GH) is True
    resumed_count = await _wait_for_raw_rows(session_factory, more_than=frozen_count)
    assert resumed_count > frozen_count, "live rows did not resume after restart"

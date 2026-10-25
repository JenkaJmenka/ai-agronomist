"""The injection collision contract on a live PostgreSQL server.

``test_synthetic_injection_collision.py`` and its ``_boundaries`` sibling run
on SQLite, which is exactly where the dialect-sensitive half of this fix is
invisible. ``persist_injection_results`` decides which raw rows to write, and
which anomaly positions landed, by matching ``normalize_ts`` against the
timestamps its ``ON CONFLICT ... RETURNING`` echoed back — and ``normalize_ts``
exists precisely because the two backends disagree: SQLite's
``DateTime(timezone=True)`` may hand back a naive datetime where Postgres
hands back an aware one.

Why that has to be covered here rather than inferred from the SQLite run: if
the normalization were wrong on Postgres, no echoed timestamp would match any
``normalize_ts(row.timestamp)``, so *every* raw row would be filtered out and
*every* anomaly position would be judged skipped. ``raw_injected`` and
``anomalies`` would both read ``0`` and nothing would raise. A silent zero on
the backend production actually runs, green on the backend the suite runs.
That is what the ``> 0`` assertions below are for.

Opt-in via ``POSTGRES_TEST_URL`` and skipped when no server is reachable —
same shape as ``tests/db/test_anomaly_episodes_constraints.py``, whose
docstring states the general principle this module is an instance of. Note
``.github/workflows/postgres-tests.yml`` fails the build if any
``postgres``-marked test *skips* there, so a skip is a local-only outcome.

Grid/window/seed constants and the seeders come from
``_synthetic_collision_fixtures.py`` — they are dialect-neutral, and reusing
them is what keeps the collision arithmetic stated in one place.
"""

from __future__ import annotations

import os
import random

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import Base, RawReading, SensorReading
from tests.api._synthetic_collision_fixtures import (
    COLLIDING_INDEXES,
    COLLISION_SEED,
    PANEL_TOTAL,
    REAL_SOURCE,
    candidate_timestamps,
    count_synthetic,
    inject_panel,
    naive_utc,
    real_row_at,
    real_sensor_values,
    seed_real_grid,
    synthetic_raw_timestamps,
    synthetic_sensor_timestamps,
)

_DEFAULT_PG_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/test_db"


@pytest.fixture
async def postgres_db() -> AsyncSession:
    """A live PostgreSQL session, or a skip.

    ``POSTGRES_TEST_URL`` is read before ``DATABASE_URL`` because
    ``tests/conftest.py`` points the latter at the SQLite test database before
    any test module is imported; reading ``DATABASE_URL`` first would skip this
    unconditionally — green, and exercising nothing.
    """
    url = os.environ.get(
        "POSTGRES_TEST_URL", os.environ.get("DATABASE_URL", _DEFAULT_PG_URL)
    )
    if not url.startswith("postgresql"):
        pytest.skip(f"not a PostgreSQL URL: {url}")

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"PostgreSQL unavailable at {url}: {exc}")

    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed_and_inject(db: AsyncSession) -> tuple[list, dict]:
    """Seed the dense 5-minute grid and run one panel-default injection.

    Returns the seeded timestamps and the response payload. Shared by the
    tests below rather than repeated, because the arrangement is identical and
    only the assertion differs.
    """
    seeded = await seed_real_grid(db)
    random.seed(COLLISION_SEED)
    return seeded, await inject_panel(db)


@pytest.mark.postgres
class TestInjectionCollisionOnPostgres:
    @pytest.mark.asyncio
    async def test_should_skip_exactly_the_grid_aligned_rows(
        self, postgres_db: AsyncSession
    ) -> None:
        """The collision set is a property of the arithmetic, not the backend."""
        await _seed_and_inject(postgres_db)
        candidates = candidate_timestamps()

        inserted = await synthetic_sensor_timestamps(postgres_db)
        skipped = {naive_utc(ts) for ts in candidates} - inserted

        assert skipped == {naive_utc(candidates[i]) for i in COLLIDING_INDEXES}
        assert len(skipped) == len(COLLIDING_INDEXES)

    @pytest.mark.asyncio
    async def test_should_report_the_same_skip_count_the_sqlite_run_reports(
        self, postgres_db: AsyncSession
    ) -> None:
        _, summary = await _seed_and_inject(postgres_db)

        assert summary["requested"] == PANEL_TOTAL
        assert summary["skipped_collisions"] == len(COLLIDING_INDEXES)
        assert summary["injected"] == await count_synthetic(postgres_db, SensorReading)
        assert summary["raw_injected"] == await count_synthetic(postgres_db, RawReading)

    @pytest.mark.asyncio
    async def test_should_not_silently_zero_raw_rows_and_anomalies(
        self, postgres_db: AsyncSession
    ) -> None:
        """The reason this module exists.

        A zero in either count here does NOT mean "nothing collided" — the
        preceding tests pin that exactly 4 of 104 rows collide, so 100 sensor
        rows inserted and their raw siblings and anomaly positions must follow.
        A zero means the ``RETURNING`` timestamp round trip stopped matching
        ``normalize_ts``, so every raw row was filtered out and every anomaly
        position judged skipped. That failure raises nothing and is invisible
        on SQLite; these two assertions are the only thing that catches it.
        """
        _, summary = await _seed_and_inject(postgres_db)

        assert summary["raw_injected"] > 0, (
            "aware/naive mismatch on the RETURNING round trip would filter out "
            "every raw row without raising"
        )
        assert summary["anomalies"] > 0, (
            "aware/naive mismatch would judge every anomaly position skipped "
            "without raising"
        )
        assert summary["anomalies"] + summary["normal"] == summary["injected"]

    @pytest.mark.asyncio
    async def test_should_keep_sensor_and_raw_rows_paired(
        self, postgres_db: AsyncSession
    ) -> None:
        """Pairing depends on the same round trip, so it is re-pinned here."""
        await _seed_and_inject(postgres_db)

        inserted = await synthetic_sensor_timestamps(postgres_db)
        assert inserted, "guard: the run must have inserted something to pair"
        assert await synthetic_raw_timestamps(postgres_db) == inserted

    @pytest.mark.asyncio
    async def test_should_leave_a_collided_real_reading_completely_untouched(
        self, postgres_db: AsyncSession
    ) -> None:
        """DO NOTHING, not DO UPDATE — asserted on the production backend.

        ``SensorReading.PROVENANCE_TRACKED`` is ``True``, so a ``DO UPDATE``
        would apply the ADR-010 provenance rule and relabel this real row.
        Postgres is where that rule actually runs in anger.
        """
        seeded, _ = await _seed_and_inject(postgres_db)
        expected = real_sensor_values(0)

        row = await real_row_at(postgres_db, seeded[0])

        assert row is not None
        assert row.source == REAL_SOURCE
        assert row.anomaly_type is None
        assert row.temperature == pytest.approx(expected["temperature"])
        assert row.humidity == pytest.approx(expected["humidity"])
        assert row.co2 == pytest.approx(expected["co2"])
        assert row.light == pytest.approx(expected["light"])

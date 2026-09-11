"""PostgreSQL-only: migration 023's non-finite (NaN / +-Inf) guard.

Why this cannot live in ``tests/api/test_023_migration.py``
-----------------------------------------------------------
SQLite coerces ``float('nan')`` to NULL on insert into a REAL column, so the
stored-NaN premise is unreachable there — the row comes back with
``anomaly_score IS NULL`` and the backfill's ``anomaly_score IS NOT NULL``
filter skips it entirely.  The SQLite twin of this test is marked xfail for
exactly that reason.

Postgres ``float8`` stores NaN and +-Infinity faithfully, which is the whole
point of the guard: feeding a non-finite score through the canonical transform
propagates it into JSON responses that then fail to serialise
(``invalid input syntax for type json / Token "NaN" is invalid``).

This drives the *real* helper out of the migration module rather than an inline
copy, via ``run_sync`` — the same bridge ``alembic/env.py`` uses to run sync
migration code over an asyncpg connection.

Local:
    POSTGRES_TEST_URL=postgresql+asyncpg://postgres@localhost:5432/test_db \\
        venv/bin/python -m pytest tests/db/test_023_nonfinite_postgres.py \\
        -m postgres -v --override-ini="addopts="

Requires the schema at revision 023 or later (``alembic upgrade head``).

CI: see .github/workflows/postgres-tests.yml
"""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.postgres

_TENANT = "nonfinite_guard_probe"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "023_canonical_anomaly_score.py"
)


def _load_migration_module():
    """Import the migration by path — alembic/versions is not a package."""
    spec = importlib.util.spec_from_file_location("_mig023", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _database_url() -> str | None:
    """Resolve the PostgreSQL URL, or None when no Postgres is configured.

    POSTGRES_TEST_URL first: ``tests/conftest.py`` overwrites DATABASE_URL with
    the SQLite test database before any test module is imported, so reading
    DATABASE_URL alone would skip this file even in the CI job built to run it.
    """
    url = os.environ.get("POSTGRES_TEST_URL") or os.environ.get("DATABASE_URL", "")
    return url if url.startswith("postgresql") else None


@pytest_asyncio.fixture()
async def pg_engine():
    url = _database_url()
    if url is None:
        pytest.skip("DATABASE_URL is not a PostgreSQL URL")
    engine = create_async_engine(url, poolclass=None)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM sensor_readings WHERE tenant_id = :t"),
                {"t": _TENANT},
            )
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM sensor_readings WHERE tenant_id = :t"),
                {"t": _TENANT},
            )
        await engine.dispose()


async def _insert_live_row(engine, timestamp: datetime, score_sql: str) -> None:
    """Insert one live-cohort row whose anomaly_score is a SQL float literal."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sensor_readings "
                "(tenant_id, greenhouse_id, timestamp, temperature, humidity, "
                " co2, light, anomaly_score, source) "
                f"VALUES (:t, 'gh', :ts, 20, 60, 400, 100, {score_sql}, 'live')"
            ),
            {"t": _TENANT, "ts": timestamp},
        )


async def _fetch_row(engine, timestamp: datetime) -> dict:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT anomaly_score, anomaly_score_raw, score_scale "
                "FROM sensor_readings WHERE tenant_id = :t AND timestamp = :ts"
            ),
            {"t": _TENANT, "ts": timestamp},
        )
        return dict(result.mappings().one())


async def _run_backfill_live(engine) -> None:
    module = _load_migration_module()
    async with engine.begin() as conn:
        await conn.run_sync(module._backfill_live)


@pytest.mark.parametrize(
    "label, score_sql",
    [
        ("nan", "CAST('NaN' AS double precision)"),
        ("posinf", "CAST('Infinity' AS double precision)"),
        ("neginf", "CAST('-Infinity' AS double precision)"),
    ],
)
async def test_non_finite_live_score_becomes_null_with_unknown_scale(
    pg_engine, label, score_sql
):
    """A non-finite stored score is neutralised to NULL / 'unknown'.

    This is the assertion SQLite cannot make: the row must genuinely hold a
    non-finite float before the backfill runs.
    """
    # asyncpg binds timestamptz from datetime objects, not ISO strings.
    second = ["nan", "posinf", "neginf"].index(label)
    timestamp = datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)
    await _insert_live_row(pg_engine, timestamp, score_sql)

    # Precondition: Postgres really did store the non-finite value. If this
    # fails the test is vacuous, which is precisely the SQLite failure mode.
    stored = await _fetch_row(pg_engine, timestamp)
    assert stored["anomaly_score"] is not None, (
        f"{label}: the row did not retain a non-finite score — this backend "
        "cannot exercise the guard"
    )

    await _run_backfill_live(pg_engine)

    row = await _fetch_row(pg_engine, timestamp)
    assert row["score_scale"] == "unknown", (
        f"{label}: expected score_scale 'unknown', got {row['score_scale']!r}"
    )
    assert row["anomaly_score"] is None, (
        f"{label}: expected NULL anomaly_score, got {row['anomaly_score']!r}"
    )
    assert row["anomaly_score_raw"] is None, (
        f"{label}: a non-finite value must not be copied into the provenance "
        f"column, got {row['anomaly_score_raw']!r}"
    )


async def test_finite_live_score_is_still_canonicalised(pg_engine):
    """Contrast case: the guard must not swallow ordinary finite scores."""
    timestamp = datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
    await _insert_live_row(pg_engine, timestamp, "0.2")

    await _run_backfill_live(pg_engine)

    row = await _fetch_row(pg_engine, timestamp)
    assert row["score_scale"] == "live"
    assert row["anomaly_score_raw"] == pytest.approx(0.2)
    # p = 0.05 (LIVE_ANOMALY_THRESHOLD), unbounded branch: above the decision
    # point, so canonical must land above 0.5.
    assert row["anomaly_score"] > 0.5

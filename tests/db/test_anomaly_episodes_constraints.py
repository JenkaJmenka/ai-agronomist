"""Item 16: the two ``anomaly_episodes`` constraints, on SQLite and Postgres.

``docs/handoff/task3_4_spec.md`` §4 and §7:

* ``ck_anomaly_episodes_release_ts_null_while_ongoing`` — CHECK
  ``(NOT is_ongoing OR release_ts IS NULL)``. ONE direction only: an ongoing
  fault cannot already carry a release timestamp, but a closed fault is NOT
  required to have one. Drift and band-breach episodes legitimately close
  without ever observing a first normal sample, so the converse must stay
  unenforced — the "accepted" cases below are as much a part of this
  constraint's contract as the rejected one.
* ``uq_anomaly_episodes_tenant_gh_channel_detector_onset`` — the upsert key.
  A second row on the same key is rejected, which is what makes a re-upload
  of an already-ingested file idempotent instead of duplicating episodes.

Both backends matter: SQLite is what the suite runs on and Postgres is what
production runs on, and a CHECK that only one of them enforces is a constraint
that only one of them has. The Postgres half is opt-in via ``POSTGRES_TEST_URL``
and skips when no server is reachable.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.db.models import AnomalyEpisodeRecord, Base

TENANT = "episode_constraints"
GH_ID = "gh_episode_constraints"
ONSET = datetime(2027, 4, 1, 6, 0, tzinfo=timezone.utc)
END = ONSET + timedelta(hours=8)
RELEASE = END + timedelta(minutes=5)

_DEFAULT_PG_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/test_db"


def _episode_kwargs(**overrides: Any) -> dict[str, Any]:
    """A valid released stuck-fault episode, before any override."""
    row: dict[str, Any] = {
        "tenant_id": TENANT,
        "greenhouse_id": GH_ID,
        "channel": "humidity",
        "detector": "stuck_fault",
        "fault_type": "stuck_fault",
        "onset_ts": ONSET,
        "end_ts": END,
        "detected_ts": ONSET + timedelta(hours=6),
        "release_ts": RELEASE,
        "duration_seconds": int((END - ONSET).total_seconds()),
        "frozen_sample_count": 97,
        "raw_slot_count": 97,
        "severity": "urgent",
        "trigger_value": 100.0,
        "is_ongoing": False,
        "source": None,
    }
    row.update(overrides)
    return row


async def _insert(db: AsyncSession, **overrides: Any) -> None:
    db.add(AnomalyEpisodeRecord(**_episode_kwargs(**overrides)))
    await db.flush()


@pytest.fixture
async def sqlite_db() -> AsyncSession:
    """An in-memory SQLite session with the full schema."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def postgres_db() -> AsyncSession:
    """A live PostgreSQL session, or a skip.

    ``POSTGRES_TEST_URL`` is read first because ``tests/conftest.py`` points
    ``DATABASE_URL`` at the SQLite test database before any test module is
    imported, which would otherwise skip this fixture unconditionally — green,
    and exercising nothing.
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

    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# =========================================================================
# The CHECK: an ongoing fault cannot already have released
# =========================================================================


class TestReleaseTsCheckOnSqlite:
    async def test_ongoing_with_a_release_ts_is_rejected(
        self, sqlite_db: AsyncSession
    ) -> None:
        """The contradictory row: still frozen, yet already released."""
        with pytest.raises(IntegrityError):
            await _insert(sqlite_db, is_ongoing=True, release_ts=RELEASE)
        await sqlite_db.rollback()
        assert await sqlite_db.scalar(select(AnomalyEpisodeRecord.id)) is None

    async def test_ongoing_without_a_release_ts_is_accepted(
        self, sqlite_db: AsyncSession
    ) -> None:
        """An open freeze: ``release_ts`` is NULL because it has not happened."""
        await _insert(sqlite_db, is_ongoing=True, release_ts=None)
        assert await sqlite_db.scalar(select(AnomalyEpisodeRecord.id)) is not None

    async def test_closed_with_a_release_ts_is_accepted(
        self, sqlite_db: AsyncSession
    ) -> None:
        """The ordinary released stuck-fault episode."""
        await _insert(sqlite_db)
        assert await sqlite_db.scalar(select(AnomalyEpisodeRecord.id)) is not None

    async def test_closed_without_a_release_ts_is_accepted(
        self, sqlite_db: AsyncSession
    ) -> None:
        """The legitimate closed-but-unreleased case the CHECK must allow.

        Tightening the constraint to require a release on every closed episode
        would reject this, which is why it is enforced in one direction only.
        """
        await _insert(sqlite_db, is_ongoing=False, release_ts=None)
        assert await sqlite_db.scalar(select(AnomalyEpisodeRecord.id)) is not None


# =========================================================================
# The unique constraint: the upsert key
# =========================================================================


class TestUpsertKeyOnSqlite:
    async def test_duplicate_key_is_rejected(self, sqlite_db: AsyncSession) -> None:
        """Two rows on the same (tenant, gh, channel, detector, onset)."""
        await _insert(sqlite_db)
        with pytest.raises(IntegrityError):
            await _insert(sqlite_db)
        await sqlite_db.rollback()

    async def test_duplicate_is_rejected_even_with_different_spans(
        self, sqlite_db: AsyncSession
    ) -> None:
        """Only the key columns decide: a longer span is still the same episode."""
        await _insert(sqlite_db)
        with pytest.raises(IntegrityError):
            await _insert(
                sqlite_db,
                end_ts=END + timedelta(hours=2),
                duration_seconds=1,
                frozen_sample_count=1,
                raw_slot_count=1,
            )
        await sqlite_db.rollback()

    @pytest.mark.parametrize(
        ("field", "value"),
        (
            ("tenant_id", "another_tenant"),
            ("greenhouse_id", "gh_elsewhere"),
            ("channel", "temperature"),
            ("detector", "band_breach"),
            ("onset_ts", ONSET + timedelta(minutes=5)),
        ),
    )
    async def test_differing_on_one_key_column_is_a_separate_episode(
        self, sqlite_db: AsyncSession, field: str, value: Any
    ) -> None:
        """Each key column discriminates: all five are part of the key."""
        await _insert(sqlite_db)
        await _insert(sqlite_db, **{field: value})
        rows = (await sqlite_db.execute(select(AnomalyEpisodeRecord))).scalars().all()
        assert len(rows) == 2

    async def test_same_key_with_a_different_fault_type_is_still_a_duplicate(
        self, sqlite_db: AsyncSession
    ) -> None:
        """``fault_type`` is NOT part of the key — and has no CHECK either.

        ``docs/handoff/task3_4_spec.md`` §5: a typo'd ``fault_type`` is
        accepted by the database by design, and validation lives in the
        writer. Pinned so the known gap stays a decision rather than a
        surprise.
        """
        await _insert(sqlite_db)
        with pytest.raises(IntegrityError):
            await _insert(sqlite_db, fault_type="typo_not_a_real_fault")
        await sqlite_db.rollback()

    async def test_an_arbitrary_fault_type_is_accepted_by_the_database(
        self, sqlite_db: AsyncSession
    ) -> None:
        """The companion half of the above: no DB vocabulary check exists."""
        await _insert(
            sqlite_db, fault_type="typo_not_a_real_fault", channel="co2"
        )
        assert (
            await sqlite_db.scalar(
                select(AnomalyEpisodeRecord.fault_type).where(
                    AnomalyEpisodeRecord.channel == "co2"
                )
            )
            == "typo_not_a_real_fault"
        )


class TestOngoingIndexIsPartialOnSqlite:
    async def test_ongoing_index_carries_its_where_clause(
        self, sqlite_db: AsyncSession
    ) -> None:
        """The open-episode index is partial on SQLite, not just on Postgres.

        ``sqlite_where`` is set alongside ``postgresql_where``; without it the
        index would silently cover every closed episode too.
        """
        sql = await sqlite_db.scalar(
            text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='ix_anomaly_episodes_ongoing_tenant_gh'"
            )
        )
        assert sql is not None
        assert sql.strip().endswith("WHERE is_ongoing")


# =========================================================================
# The same two constraints on a live PostgreSQL server (opt-in)
# =========================================================================


@pytest.mark.postgres
class TestConstraintsOnPostgres:
    async def test_ongoing_with_a_release_ts_is_rejected(
        self, postgres_db: AsyncSession
    ) -> None:
        """The CHECK is enforced by the production backend as well."""
        with pytest.raises(IntegrityError):
            await _insert(postgres_db, is_ongoing=True, release_ts=RELEASE)
        await postgres_db.rollback()

    async def test_closed_without_a_release_ts_is_accepted(
        self, postgres_db: AsyncSession
    ) -> None:
        """And it is enforced in one direction there too."""
        await _insert(postgres_db, is_ongoing=False, release_ts=None)
        assert await postgres_db.scalar(select(AnomalyEpisodeRecord.id)) is not None

    async def test_duplicate_upsert_key_is_rejected(
        self, postgres_db: AsyncSession
    ) -> None:
        """The upsert key is unique on Postgres: re-uploads cannot duplicate."""
        await _insert(postgres_db)
        with pytest.raises(IntegrityError):
            await _insert(postgres_db)
        await postgres_db.rollback()

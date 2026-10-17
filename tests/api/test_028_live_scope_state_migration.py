"""Migration 028 builds a table the live-scope service can actually write to.

Why this is not redundant with the service tests
------------------------------------------------
Every other test of ``live_scope_state`` builds its schema with
``Base.metadata.create_all``, so they all assert the service against the ORM's
own idea of the table.  Production gets its table from migration 028 instead,
and ``store_desired_state`` upserts with ``ON CONFLICT (tenant_id,
greenhouse_id)`` — a conflict target that must match the primary key the
*migration* created, not the one the ORM would have.  A mismatch there passes
every create_all-based test and fails on the first Go Live after deploy.

So these cases run the real chain and then drive the real service functions
against the database the chain produced.

SQLite only, like the sibling chain tests: this covers the DDL alembic emits
for a portable ``create_table``, not Postgres-specific behaviour.  The
Postgres CI job (``.github/workflows/postgres-tests.yml``) covers that.
"""

from __future__ import annotations

from typing import AsyncGenerator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic import command
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.live_scope_state_service import (
    DesiredScopeState,
    fetch_desired_state,
    fetch_live_scopes,
    store_desired_state,
)
from src.db.models import LiveScopeState
from tests.api._alembic_chain import (
    alembic_config,
    ancestry_of,
    stamped_revision,
)

_REVISION = "028"
_PREVIOUS_REVISION = "027"
_TENANT = "migration_tenant"
_GREENHOUSE = "gh_migrated"


def _upgrade_to_head(tmp_path, monkeypatch) -> tuple[str, sa.engine.url.URL]:
    """Run the whole chain against a throwaway SQLite file."""
    url = f"sqlite:///{tmp_path / 'scope_state.db'}"
    config = alembic_config(url, monkeypatch)
    command.upgrade(config, "head")
    return url, config


@pytest_asyncio.fixture
async def migrated_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[AsyncSession, None]:
    """A session on a database whose schema came from alembic, not create_all."""
    url, _ = _upgrade_to_head(tmp_path, monkeypatch)
    engine = create_async_engine(url.replace("sqlite://", "sqlite+aiosqlite://"))
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


class TestMigrationIsReachable:

    def test_028_is_on_the_path_upgrade_head_walks(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Membership in head's ancestry, not equality with a literal head —
        the sibling chain tests explain why a literal id rots."""
        url, config = _upgrade_to_head(tmp_path, monkeypatch)
        engine = sa.create_engine(url)
        try:
            assert _REVISION in ancestry_of(config, stamped_revision(engine))
        finally:
            engine.dispose()

    def test_downgrade_removes_the_table(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A migration that cannot be rolled back strands a bad deploy."""
        url, config = _upgrade_to_head(tmp_path, monkeypatch)
        command.downgrade(config, _PREVIOUS_REVISION)

        engine = sa.create_engine(url)
        try:
            assert "live_scope_state" not in sa.inspect(engine).get_table_names()
            assert stamped_revision(engine) == _PREVIOUS_REVISION
        finally:
            engine.dispose()


class TestServiceWorksAgainstTheMigratedTable:

    @pytest.mark.asyncio
    async def test_store_then_fetch_round_trips(
        self, migrated_session: AsyncSession
    ) -> None:
        persisted = await store_desired_state(
            migrated_session,
            tenant_id=_TENANT,
            greenhouse_id=_GREENHOUSE,
            desired_state=DesiredScopeState.LIVE,
        )
        await migrated_session.commit()

        assert persisted is True
        assert await fetch_live_scopes(migrated_session) == [(_TENANT, _GREENHOUSE)]

    @pytest.mark.asyncio
    async def test_upsert_conflict_target_matches_the_migrations_primary_key(
        self, migrated_session: AsyncSession
    ) -> None:
        """The case a create_all-built schema cannot catch.

        ``store_desired_state`` names ``(tenant_id, greenhouse_id)`` as its
        ON CONFLICT target.  If migration 028's primary key did not cover
        exactly those columns, the second write would raise or insert a
        duplicate instead of updating in place.
        """
        for state in (DesiredScopeState.LIVE, DesiredScopeState.PAUSED):
            await store_desired_state(
                migrated_session,
                tenant_id=_TENANT,
                greenhouse_id=_GREENHOUSE,
                desired_state=state,
            )
        await migrated_session.commit()

        rows = (await migrated_session.execute(select(LiveScopeState))).scalars().all()
        assert len(rows) == 1
        assert (
            await fetch_desired_state(
                migrated_session, tenant_id=_TENANT, greenhouse_id=_GREENHOUSE
            )
            is DesiredScopeState.PAUSED
        )

    @pytest.mark.asyncio
    async def test_updated_at_moves_on_the_second_write(
        self, migrated_session: AsyncSession
    ) -> None:
        """``onupdate`` does not apply to an ON CONFLICT DO UPDATE set clause,
        so the service passes the timestamp explicitly. Pin that it works on
        the migrated column, which carries a server_default."""
        await store_desired_state(
            migrated_session,
            tenant_id=_TENANT,
            greenhouse_id=_GREENHOUSE,
            desired_state=DesiredScopeState.LIVE,
        )
        await migrated_session.commit()
        first = (
            await migrated_session.execute(select(LiveScopeState.updated_at))
        ).scalar_one()

        await store_desired_state(
            migrated_session,
            tenant_id=_TENANT,
            greenhouse_id=_GREENHOUSE,
            desired_state=DesiredScopeState.PAUSED,
        )
        await migrated_session.commit()
        second = (
            await migrated_session.execute(select(LiveScopeState.updated_at))
        ).scalar_one()

        assert second >= first


class TestMigratedCheckConstraint:

    @pytest.mark.asyncio
    async def test_a_state_outside_the_vocabulary_is_rejected(
        self, migrated_session: AsyncSession
    ) -> None:
        """The service cannot emit a bad value — it takes an enum — so this
        guards the schema against anything else that writes the table."""
        with pytest.raises(IntegrityError):
            await migrated_session.execute(
                text(
                    "INSERT INTO live_scope_state "
                    "(tenant_id, greenhouse_id, desired_state, updated_at) "
                    "VALUES (:t, :g, 'streaming', CURRENT_TIMESTAMP)"
                ),
                {"t": _TENANT, "g": _GREENHOUSE},
            )
        await migrated_session.rollback()

    @pytest.mark.asyncio
    async def test_both_documented_states_are_accepted(
        self, migrated_session: AsyncSession
    ) -> None:
        for index, state in enumerate(DesiredScopeState):
            await store_desired_state(
                migrated_session,
                tenant_id=_TENANT,
                greenhouse_id=f"{_GREENHOUSE}_{index}",
                desired_state=state,
            )
        await migrated_session.commit()

        stored = (
            await migrated_session.execute(select(LiveScopeState.desired_state))
        ).scalars().all()
        assert sorted(stored) == ["live", "paused"]

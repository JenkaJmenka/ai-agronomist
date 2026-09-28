"""PostgreSQL-only: alembic autogenerate must never propose dropping the
`auth` schema (Auth.js login/session store, owned by the `web` service).

Why this cannot live in tests/api/
-----------------------------------
This drives alembic's real comparison machinery
(``alembic.autogenerate.compare_metadata``) against a live database with a
non-default schema present, which needs ``include_schemas=True`` reflection
across multiple Postgres schemas — SQLite has no equivalent concept, so this
is genuinely Postgres-only, like the other files in this directory.

What this guards against
-------------------------
``sec-p2-web`` moves the five Auth.js tables (``users``, ``accounts``,
``sessions``, ``verification_token``, ``user_tenants``) into a dedicated
Postgres schema named ``auth``, on the same database alembic manages via
``DATABASE_URL``. alembic's own metadata (``src.db.models.Base.metadata``)
knows nothing about those tables. Without ``src/db/alembic_schema_filters.py``
wired into ``alembic/env.py``, `alembic revision --autogenerate` would
reflect that schema, see its tables as unknown-to-metadata, and propose
`DROP TABLE` for every one of them — silently destroying the user/session
store the next time someone runs autogenerate against the real database.

Per this project's own migration hazard notes (never commit inside a
migration; never validate one against an empty database), the DB-backed test
below does not trust an empty database: its precondition test asserts that
Postgres itself reports all five real tables inside the ``auth`` schema, so
"no operations proposed against `auth`" cannot pass vacuously just because
there was nothing there to propose anything about. That the filter is scoped
to ``auth`` alone (rather than a blanket exclusion that would also hide real
app-table drift) is proved separately by the pure unit tests in
``TestAlembicSchemaFiltersAreScopedNotBlanket``.

Why the async engine
--------------------
Production migrations run over **asyncpg**: ``alembic/env.py`` branches on
``"+asyncpg" in url`` and drives them through ``create_async_engine(...)`` plus
``await conn.run_sync(...)``, because asyncpg is an async-only driver. This
test uses the same async engine and the same ``run_sync`` bridge for alembic's
synchronous comparison API, so the guard covers the configuration production
actually runs. It deliberately does *not* rewrite the URL to a sync driver:
psycopg2 is optional in ``requirements.txt`` (a comment, not a dependency — it
is not installed in CI), and a sync engine would exercise a configuration that
does not exist in production.

Local:
    POSTGRES_TEST_URL=postgresql+asyncpg://postgres@localhost:5432/test_db \\
        venv/bin/python -m pytest tests/db/test_alembic_auth_schema_guard.py \\
        -m postgres -v --override-ini="addopts="

CI: see .github/workflows/postgres-tests.yml
"""

from __future__ import annotations

import ast
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from src.db.alembic_schema_filters import include_name, include_object
from src.db.models import Base
from src.db.session import _normalize_database_url

pytestmark = pytest.mark.postgres

AUTH_SCHEMA = "auth"

# Minimal equivalent of web/lib/auth/schema.sql's DDL — deliberately not
# imported from web/ (out of scope for this task); just enough real table
# structure (primary keys, a foreign key, a unique constraint) to exercise
# reflection realistically, not just a bare CREATE TABLE.
_AUTH_SCHEMA_DDL = f"""
create schema if not exists {AUTH_SCHEMA};

create table if not exists {AUTH_SCHEMA}.users (
  id uuid primary key default gen_random_uuid(),
  name text,
  email text unique not null,
  "emailVerified" timestamptz,
  image text
);

create table if not exists {AUTH_SCHEMA}.accounts (
  id uuid primary key default gen_random_uuid(),
  "userId" uuid not null references {AUTH_SCHEMA}.users (id) on delete cascade,
  type text not null,
  provider text not null,
  "providerAccountId" text not null,
  unique (provider, "providerAccountId")
);

create table if not exists {AUTH_SCHEMA}.sessions (
  id uuid primary key default gen_random_uuid(),
  "userId" uuid not null references {AUTH_SCHEMA}.users (id) on delete cascade,
  expires timestamptz not null,
  "sessionToken" text unique not null
);

create table if not exists {AUTH_SCHEMA}.verification_token (
  identifier text not null,
  expires timestamptz not null,
  token text not null,
  primary key (identifier, token)
);

create table if not exists {AUTH_SCHEMA}.user_tenants (
  email text primary key,
  tenant_id text not null,
  created_at timestamptz not null default now()
);
"""

_AUTH_TABLE_NAMES = frozenset(
    {"users", "accounts", "sessions", "verification_token", "user_tenants"}
)


def _async_database_url() -> str | None:
    """Resolve the async PostgreSQL URL, or None when unconfigured.

    POSTGRES_TEST_URL first: tests/conftest.py overwrites DATABASE_URL with
    the SQLite test database before any test module is imported.  The URL is
    left on its async driver and normalised through the same
    ``_normalize_database_url`` ``alembic/env.py`` calls, so this test connects
    the way production migrations do.
    """
    url = os.environ.get("POSTGRES_TEST_URL") or os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return None
    return url


@pytest_asyncio.fixture
async def pg_engine_with_auth_schema() -> AsyncIterator[AsyncEngine]:
    """An async engine bound to a database that has the real `auth` schema.

    Mirrors ``alembic/env.py``'s ``_run_async_migrations``: async engine,
    ``NullPool``, and every synchronous step bridged through ``run_sync``.
    """
    url = _async_database_url()
    if url is None:
        pytest.skip("No PostgreSQL URL configured")
    clean_url, connect_args = _normalize_database_url(url)
    engine = create_async_engine(
        clean_url, connect_args=connect_args, poolclass=pool.NullPool
    )
    try:
        async with engine.begin() as conn:
            # One statement per execute: asyncpg sends statements through the
            # prepared-statement protocol, which rejects multi-command strings.
            for statement in _AUTH_SCHEMA_DDL.strip().split(";\n\n"):
                statement = statement.strip()
                if statement:
                    await conn.execute(text(statement))
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {AUTH_SCHEMA} CASCADE"))
        await engine.dispose()


async def _reflected_auth_tables(engine: AsyncEngine) -> set[str]:
    """Table names Postgres itself reports inside the `auth` schema."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema"
            ),
            {"schema": AUTH_SCHEMA},
        )
        return {row[0] for row in result}


def _compare_with_env_filters(connection: Connection) -> list:
    """Run the same comparison alembic's `revision --autogenerate` would,
    with the exact filters wired into alembic/env.py.

    Synchronous by necessity — alembic's autogenerate API has no async form,
    which is precisely why ``alembic/env.py`` hands a sync connection to
    ``context.configure`` via ``run_sync`` when the driver is asyncpg.
    """
    migration_context = MigrationContext.configure(
        connection=connection,
        opts={
            "target_metadata": Base.metadata,
            "include_schemas": True,
            "include_name": include_name,
            "include_object": include_object,
        },
    )
    return compare_metadata(migration_context, Base.metadata)


async def _autogenerate_diff(engine: AsyncEngine) -> list:
    async with engine.connect() as conn:
        return await conn.run_sync(_compare_with_env_filters)


def _schema_of(op: tuple) -> str | None:
    """Best-effort extraction of the Postgres schema an autogenerate op
    touches, across the several op-tuple shapes compare_metadata returns."""
    for item in op[1:]:
        schema = getattr(item, "schema", None)
        if schema is not None:
            return schema
        table = getattr(item, "table", None)
        if table is not None and getattr(table, "schema", None) is not None:
            return table.schema
    return None


class TestAlembicNeverTouchesAuthSchema:
    async def test_fixture_actually_created_the_auth_tables(
        self, pg_engine_with_auth_schema: AsyncEngine
    ) -> None:
        """Precondition guard (this project's own hazard note): if this
        fails, every assertion below would pass vacuously against an empty
        schema, which is exactly the false-green this test exists to avoid.
        """
        found = await _reflected_auth_tables(pg_engine_with_auth_schema)
        assert found == _AUTH_TABLE_NAMES, (
            f"auth schema fixture did not create the expected tables: "
            f"found {found}, expected {_AUTH_TABLE_NAMES}"
        )

    async def test_autogenerate_proposes_no_change_to_auth_schema(
        self, pg_engine_with_auth_schema: AsyncEngine
    ) -> None:
        diff = await _autogenerate_diff(pg_engine_with_auth_schema)

        auth_touching_ops = [op for op in diff if _schema_of(op) == AUTH_SCHEMA]
        assert auth_touching_ops == [], (
            "alembic autogenerate proposed operations against the `auth` "
            f"schema — this would drop/alter the Auth.js store: {auth_touching_ops!r}"
        )


class TestAlembicSchemaFiltersAreScopedNotBlanket:
    """Pure unit tests of the two hooks — no DB, no postgres marker, always
    runs. Proves the filter excludes only the `auth` schema and nothing
    else, independent of whatever migration state a given database happens
    to be in (the DB-backed tests above intentionally don't assert this,
    since a fully-migrated database and a fresh one produce very different
    diffs for the app's own tables).
    """

    def test_include_name_excludes_only_the_auth_schema(self) -> None:
        assert include_name("auth", "schema", {}) is False
        assert include_name("public", "schema", {}) is True
        assert include_name(None, "schema", {}) is True
        # Non-schema types are never filtered by name (schema exclusion
        # happens once, at the schema level, not re-decided per object).
        assert include_name("auth", "table", {}) is True

    def test_include_object_excludes_only_auth_schema_tables(self) -> None:
        auth_table = SimpleNamespace(schema="auth")
        public_table = SimpleNamespace(schema=None)

        assert include_object(auth_table, "users", "table", False, None) is False
        assert include_object(public_table, "sensor_readings", "table", False, None) is True

    def test_include_object_excludes_columns_of_auth_schema_tables(self) -> None:
        auth_column = SimpleNamespace(table=SimpleNamespace(schema="auth"))
        public_column = SimpleNamespace(table=SimpleNamespace(schema=None))

        assert include_object(auth_column, "email", "column", False, None) is False
        assert include_object(public_column, "tenant_id", "column", False, None) is True

    def test_include_object_defaults_to_true_for_unrecognised_types(self) -> None:
        # Object types this filter doesn't special-case (e.g. a top-level
        # "schema" object reaching include_object) must not be excluded —
        # only include_name makes the schema-level call.
        assert include_object(SimpleNamespace(), "auth", "schema", False, None) is True


_ALEMBIC_ENV_PY = Path(__file__).resolve().parents[2] / "alembic" / "env.py"

_REQUIRED_CONFIGURE_KEYWORDS = frozenset(
    {"include_schemas", "include_name", "include_object"}
)


def _context_configure_keywords() -> list[dict[str, ast.expr]]:
    """Keyword arguments of every ``context.configure(...)`` call in
    ``alembic/env.py``, one dict per call site.

    Parsed statically with ``ast`` rather than imported: ``alembic/env.py``
    runs a real migration as a side effect of being imported, so no test may
    import it (the module says so itself).
    """
    tree = ast.parse(_ALEMBIC_ENV_PY.read_text())
    calls = []
    for node in ast.walk(tree):
        is_context_configure = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "configure"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "context"
        )
        if is_context_configure:
            calls.append({kw.arg: kw.value for kw in node.keywords if kw.arg})
    return calls


class TestAlembicEnvWiresTheFiltersItself:
    """The DB-backed guard above configures the two hooks itself, because
    ``alembic/env.py`` cannot be imported from a test — which means deleting
    ``include_name``/``include_object`` from ``env.py`` would leave that guard
    green while the real ``alembic revision --autogenerate`` drops the Auth.js
    tables. This class closes that gap statically: it fails if the production
    migration entrypoint stops passing the filters (verified by deliberate
    break).
    """

    def test_every_context_configure_passes_the_filters(self) -> None:
        configure_calls = _context_configure_keywords()
        assert configure_calls, (
            f"no context.configure(...) call found in {_ALEMBIC_ENV_PY} — "
            "the parser below can no longer see how migrations are configured"
        )
        for keywords in configure_calls:
            missing = _REQUIRED_CONFIGURE_KEYWORDS - keywords.keys()
            assert not missing, (
                f"alembic/env.py has a context.configure(...) call missing "
                f"{sorted(missing)} — autogenerate there would reflect the "
                f"`{AUTH_SCHEMA}` schema and propose dropping the Auth.js store"
            )

    def test_filters_are_the_shared_hooks_and_schemas_are_included(self) -> None:
        for keywords in _context_configure_keywords():
            assert keywords["include_schemas"].value is True
            # The same callables this test imports from
            # src/db/alembic_schema_filters.py, not a local re-implementation.
            assert keywords["include_name"].id == include_name.__name__
            assert keywords["include_object"].id == include_object.__name__

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
migration; never validate one against an empty database), this test does
not validate against an empty database: it asserts the `auth` schema
fixture actually created real tables before asserting anything about the
diff, and it asserts the diff contains *some* unrelated app-table op too
(proving the comparison ran a real, non-trivial reflection rather than
vacuously seeing nothing at all).

Local:
    POSTGRES_TEST_URL=postgresql+asyncpg://postgres@localhost:5432/test_db \\
        venv/bin/python -m pytest tests/db/test_alembic_auth_schema_guard.py \\
        -m postgres -v --override-ini="addopts="

CI: see .github/workflows/postgres-tests.yml
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from src.db.alembic_schema_filters import include_name, include_object
from src.db.models import Base

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


def _database_url() -> str | None:
    """Resolve a sync-driver PostgreSQL URL, or None when unconfigured.

    POSTGRES_TEST_URL first: tests/conftest.py overwrites DATABASE_URL with
    the SQLite test database before any test module is imported.  alembic's
    comparison API is synchronous, so this normalises the asyncpg-style URL
    other postgres tests use to a psycopg2 one (both are in requirements.txt).
    """
    url = os.environ.get("POSTGRES_TEST_URL") or os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return None
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@pytest.fixture()
def pg_engine_with_auth_schema() -> Engine:
    """A sync engine bound to a database that has the real `auth` schema."""
    url = _database_url()
    if url is None:
        pytest.skip("No PostgreSQL URL configured")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            for statement in _AUTH_SCHEMA_DDL.strip().split(";\n\n"):
                statement = statement.strip()
                if statement:
                    conn.execute(text(statement))
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {AUTH_SCHEMA} CASCADE"))
        engine.dispose()


def _reflected_auth_tables(engine: Engine) -> set[str]:
    """Table names Postgres itself reports inside the `auth` schema."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :schema"
            ),
            {"schema": AUTH_SCHEMA},
        )
        return {row[0] for row in result}


def _autogenerate_diff(engine: Engine) -> list:
    """Run the same comparison alembic's `revision --autogenerate` would,
    with the exact filters wired into alembic/env.py."""
    with engine.connect() as conn:
        migration_context = MigrationContext.configure(
            connection=conn,
            opts={
                "target_metadata": Base.metadata,
                "include_schemas": True,
                "include_name": include_name,
                "include_object": include_object,
            },
        )
        return compare_metadata(migration_context, Base.metadata)


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
    def test_fixture_actually_created_the_auth_tables(
        self, pg_engine_with_auth_schema: Engine
    ) -> None:
        """Precondition guard (this project's own hazard note): if this
        fails, every assertion below would pass vacuously against an empty
        schema, which is exactly the false-green this test exists to avoid.
        """
        found = _reflected_auth_tables(pg_engine_with_auth_schema)
        assert found == _AUTH_TABLE_NAMES, (
            f"auth schema fixture did not create the expected tables: "
            f"found {found}, expected {_AUTH_TABLE_NAMES}"
        )

    def test_autogenerate_proposes_no_change_to_auth_schema(
        self, pg_engine_with_auth_schema: Engine
    ) -> None:
        diff = _autogenerate_diff(pg_engine_with_auth_schema)

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

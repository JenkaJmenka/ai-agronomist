import asyncio
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.db.alembic_schema_filters import include_name, include_object
from src.db.models import Base
from src.db.session import _normalize_database_url

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False prevents alembic's ini config from
    # calling .disabled=True on every logger already created in this process.
    # Without this, running migrations in-process (e.g. in tests) silences
    # all application loggers for the remainder of the test session.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Auth.js (the `web` service's own login/session store) lives in a dedicated
# Postgres schema, fixed at "auth" on both sides, on the SAME database
# `AUTH_DATABASE_URL` points at (users/accounts/sessions/verification_token/
# user_tenants — see web/lib/auth/schema.sql). alembic's metadata
# (target_metadata above) knows nothing about those tables. Without
# `include_schemas` + the two filters below, `alembic revision
# --autogenerate` against the real database would see them as
# unknown-to-metadata and propose DROP TABLE for every one of them —
# silently destroying the user/session store on the next migration. The
# filters live in src/db/alembic_schema_filters.py (not inlined here) so
# they're directly unit-testable — this file runs a real migration as a
# side effect of being imported and must never be imported from a test.
# See tests/db/test_alembic_auth_schema_guard.py for the regression guard.

database_url = os.environ.get("DATABASE_URL")
if database_url:
    if "sqlite+aiosqlite" in database_url:
        database_url = database_url.replace("sqlite+aiosqlite", "sqlite")
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        include_schemas=True,
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations(url: str) -> None:
    clean_url, connect_args = _normalize_database_url(url)
    engine = create_async_engine(clean_url, connect_args=connect_args, poolclass=pool.NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if url and "sqlite+aiosqlite" in url:
        url = url.replace("sqlite+aiosqlite", "sqlite")
        config.set_main_option("sqlalchemy.url", url)

    if url and "+asyncpg" in url:
        # asyncpg is an async-only driver; use async engine for migrations
        asyncio.run(_run_async_migrations(url))
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

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
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
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

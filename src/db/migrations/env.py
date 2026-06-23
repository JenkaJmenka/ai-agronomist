import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# -----------------------------------------------------------------------------
# Path and Import Setup
# -----------------------------------------------------------------------------
# Add the project root to sys.path to ensure Alembic can import the 'src' package.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Import the models and DeclarativeBase to register them with metadata.
from src.db.models import Base

# This is the Alembic Config object, which provides access to the values inside alembic.ini.
config = context.config

# -----------------------------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------------------------
# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# -----------------------------------------------------------------------------
# Metadata Setup
# -----------------------------------------------------------------------------
# Set up model metadata for autogenerate support.
target_metadata = Base.metadata

# -----------------------------------------------------------------------------
# Dynamic Database Connection Setup
# -----------------------------------------------------------------------------
# Permit DATABASE_URL env var override for production, docker-compose, or CI/CD pipelines.
database_url = os.environ.get("DATABASE_URL")
if database_url:
    # Alembic runs migrations synchronously. If the app uses aiosqlite for FastAPI,
    # we convert it here to standard synchronous sqlite driver.
    if "sqlite+aiosqlite" in database_url:
        database_url = database_url.replace("sqlite+aiosqlite", "sqlite")
    config.set_main_option("sqlalchemy.url", database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though an
    Engine is also acceptable here. By omitting the Engine creation we don't
    even need a DBAPI to be available. Calls to context.execute() here emit the
    given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a connection with
    the context.
    """
    url = config.get_main_option("sqlalchemy.url")
    
    # Ensure sync connection for sqlite if it has an async prefix in config
    if url and "sqlite+aiosqlite" in url:
        url = url.replace("sqlite+aiosqlite", "sqlite")
        config.set_main_option("sqlalchemy.url", url)

    # Use a null pool or configured engine options
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # render_as_batch=True is highly critical for supporting SQLite.
            # SQLite does not support raw ALTER, ADD, or DROP operations on 
            # columns cleanly. Batch mode mimics them by copy-and-recreating tables.
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

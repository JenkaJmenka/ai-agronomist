"""
Database session management for the AI Agronomist platform.

This module configures the database connection using SQLAlchemy 2.x async,
defines the base declarative class, and provides the dependency injection
for session routing and database initialization hooks on startup.
"""

import os
from typing import AsyncGenerator
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool


def _normalize_database_url(url: str) -> tuple[str, dict]:
    """Convert provider URLs to SQLAlchemy async drivers.

    Returns (normalized_url, extra_connect_args).
    Strips sslmode from the query string and returns ssl="require" in
    connect_args instead — asyncpg does not accept sslmode as a URL param.
    """
    extra: dict = {}

    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]

    if "+asyncpg" in url and "sslmode=" in url:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        sslmode = params.pop("sslmode", [""])[0]
        if sslmode in ("require", "verify-ca", "verify-full"):
            extra["ssl"] = "require"
        url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in params.items()})))

    return url, extra


DATABASE_URL: str
_extra_connect_args: dict
DATABASE_URL, _extra_connect_args = _normalize_database_url(
    os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./greenhouse.db")
)

connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False
elif "+asyncpg" in DATABASE_URL:
    connect_args.update(_extra_connect_args)
    connect_args["timeout"] = float(os.environ.get("DB_CONNECT_TIMEOUT", "15"))

# asyncpg connections are bound to the event loop they are created on.
# Streamlit runs script threads with different loops than the module-import loop,
# so pooled asyncpg connections cannot be reused across calls. NullPool forces a
# fresh connection per call, eliminating the "Future attached to a different loop" error.
_pool_kwargs = {"poolclass": NullPool} if "+asyncpg" in DATABASE_URL else {}

# Create AsyncEngine
engine = create_async_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=False,  # Set to True for verbose SQL query logging
    **_pool_kwargs,
)

# Async session factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency injection generator to yield an asynchronous database session.
    Automatically handles commit, rollback on exceptions, and closing on completion.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def init_db() -> None:
    """
    Initializes the database schema by creating all metadata tables.
    Matches the startup hook requirement.

    Alembic migrations are the source of truth for schema; this hook only
    creates tables that are missing entirely (idempotent for existing ones).
    """
    # ORM models live on models.Base (a distinct DeclarativeBase from the
    # one exported here) — create_all must run on THAT metadata, otherwise
    # this hook silently creates nothing.
    from src.db.models import Base as models_base

    async with engine.begin() as conn:
        await conn.run_sync(models_base.metadata.create_all)

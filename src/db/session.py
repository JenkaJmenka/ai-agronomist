"""
Database session management for the AI Agronomist platform.

This module configures the database connection using SQLAlchemy 2.x async,
defines the base declarative class, and provides the dependency injection
for session routing and database initialization hooks on startup.
"""

import os
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

# Database URL configuration, defaulting to local SQLite using async aiosqlite
def _normalize_database_url(url: str) -> str:
    """Convert Render/Heroku-style URLs to SQLAlchemy async drivers."""
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


DATABASE_URL: str = _normalize_database_url(
    os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./greenhouse.db")
)

# Connection arguments for SQLite to handle multi-threaded dev environments
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

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

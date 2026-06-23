"""Fixtures for monitoring tests: async DB session and FastAPI test client."""

from __future__ import annotations

from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.models import Base
from src.db.session import get_db
from src.monitoring.anomaly_metrics import reset_anomaly_metrics


@pytest_asyncio.fixture
async def test_db() -> AsyncGenerator[AsyncSession, None]:
    """Isolated in-memory async SQLite session with all tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def client(test_db: AsyncSession) -> Generator[TestClient, None, None]:
    """TestClient wired to the in-memory DB session."""
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield test_db

    reset_anomaly_metrics()
    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_anomaly_metrics()

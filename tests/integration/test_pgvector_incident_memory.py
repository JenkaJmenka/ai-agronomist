"""PostgreSQL/pgvector integration tests for incident memory (BUG-011).

Opt-in: requires a live PostgreSQL with the pgvector extension.

Local:
  docker compose up -d postgres
  venv/bin/python -m pytest tests/integration/test_pgvector_incident_memory.py -m postgres -q \\
    --override-ini="addopts="

CI: see .github/workflows/postgres-tests.yml
"""

from __future__ import annotations

import os
from typing import List

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import LATENT_DIM, SEQ_LEN
from src.db.incident_memory import IncidentMemory
from src.db.models import Base
from src.db.vector_search import (
    compute_cosine_distance,
    find_similar_incidents,
    is_postgres_session,
)

pytestmark = pytest.mark.postgres

DEFAULT_POSTGRES_URL = (
    "postgresql+asyncpg://postgres:postgres_dev_password@localhost:5432/"
    "greenhouse_monitoring"
)


def _postgres_url() -> str:
    return os.environ.get("POSTGRES_TEST_URL", DEFAULT_POSTGRES_URL)


def unit_embedding(axis: int = 0, sign: float = 1.0) -> List[float]:
    embedding = [0.0] * LATENT_DIM
    embedding[axis] = sign
    return embedding


def sample_sequence() -> List[List[float]]:
    row = [22.0, 60.0, 400.0, 500.0]
    return [row[:] for _ in range(SEQ_LEN)]


@pytest_asyncio.fixture
async def postgres_session() -> AsyncSession:
    """Async PostgreSQL session with pgvector extension and incident tables."""
    url = _postgres_url()
    try:
        engine = create_async_engine(url, echo=False)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        try:
            await engine.dispose()
        except Exception:
            pass
        pytest.skip(f"PostgreSQL setup failed: {exc}")

    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_session_detected(postgres_session: AsyncSession) -> None:
    assert is_postgres_session(postgres_session) is True


@pytest.mark.asyncio
async def test_pgvector_similarity_search_ordering(
    postgres_session: AsyncSession,
) -> None:
    """Exercise native pgvector cosine distance (<=>) path."""
    memory = IncidentMemory(postgres_session)
    query = unit_embedding(0)

    for embedding in [unit_embedding(0, -1.0), unit_embedding(0), unit_embedding(1)]:
        await memory.store_incident(
            tenant_id="pg_tenant",
            embedding=embedding,
            anomaly_score=0.8,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
        )

    similar = await find_similar_incidents(
        postgres_session,
        embedding=query,
        tenant_id="pg_tenant",
        k=3,
    )
    assert len(similar) == 3
    distances = [distance for _, distance in similar]
    assert distances == sorted(distances)
    assert distances[0] == pytest.approx(0.0, abs=1e-5)

    # Cross-check first result against numpy reference implementation.
    first_incident, pg_distance = similar[0]
    import numpy as np

    ref_distance = compute_cosine_distance(
        np.asarray(query, dtype=np.float64),
        np.asarray(first_incident.embedding, dtype=np.float64),
    )
    assert pg_distance == pytest.approx(ref_distance, abs=1e-5)


@pytest.mark.asyncio
async def test_pgvector_tenant_isolation(postgres_session: AsyncSession) -> None:
    memory = IncidentMemory(postgres_session)
    await memory.store_incident(
        tenant_id="tenant_a",
        embedding=unit_embedding(0),
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.store_incident(
        tenant_id="tenant_b",
        embedding=unit_embedding(0),
        anomaly_score=0.5,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )

    similar = await find_similar_incidents(
        postgres_session,
        embedding=unit_embedding(0),
        tenant_id="tenant_a",
        k=10,
    )
    assert len(similar) == 1
    assert similar[0][0].tenant_id == "tenant_a"

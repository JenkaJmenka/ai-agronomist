"""PostgreSQL/pgvector integration tests for incident memory (S3-1).

Opt-in: requires a live PostgreSQL database with the pgvector extension.
Tests are skipped automatically when DATABASE_URL is not a postgres URL or
when the database is unreachable.

Local:
    docker compose up -d db
    POSTGRES_TEST_URL=postgresql+asyncpg://postgres:password@localhost:5432/anomaly_db \\
        venv/bin/python -m pytest tests/db/test_incident_memory_postgres.py \\
        -m postgres -v --override-ini="addopts="

Use POSTGRES_TEST_URL, not DATABASE_URL: tests/conftest.py pins DATABASE_URL to
SQLite for the whole suite before any app import, which would skip everything here.

CI: see .github/workflows/postgres-tests.yml
"""

from __future__ import annotations

import os
from typing import List

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
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

_DEFAULT_PG_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/test_db"


def _postgres_url() -> str:
    """Return the PostgreSQL URL from the environment.

    POSTGRES_TEST_URL is checked first because ``tests/conftest.py`` overwrites
    DATABASE_URL with the SQLite test database before any test module is
    imported.  Reading DATABASE_URL alone meant every test in this file skipped
    — including in the CI job that exists to run them, which reported green
    while exercising nothing.
    """
    return os.environ.get(
        "POSTGRES_TEST_URL", os.environ.get("DATABASE_URL", _DEFAULT_PG_URL)
    )


def _is_postgres_url(url: str) -> bool:
    return url.startswith("postgresql")


def unit_embedding(axis: int = 0, sign: float = 1.0) -> List[float]:
    """Unit vector along one latent axis."""
    embedding = [0.0] * LATENT_DIM
    embedding[axis] = sign
    return embedding


def sample_sequence() -> List[List[float]]:
    """Valid sensor sequence [seq_len, input_dim]."""
    row = [22.0, 60.0, 400.0, 500.0]
    return [row[:] for _ in range(SEQ_LEN)]


@pytest_asyncio.fixture
async def pg_db() -> AsyncSession:
    """Async PostgreSQL session with pgvector extension and incident tables.

    Skips the test if DATABASE_URL is not a PostgreSQL URL or the database
    cannot be reached.
    """
    url = _postgres_url()
    if not _is_postgres_url(url):
        pytest.skip(f"DATABASE_URL is not a PostgreSQL URL: {url}")

    engine = create_async_engine(url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except OSError as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL not reachable at {url}: {exc}")
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL setup failed: {exc}")

    Session = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ---------------------------------------------------------------------------
# Test 1: pgvector session detected correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_session_detected(pg_db: AsyncSession) -> None:
    """is_postgres_session returns True for a live PostgreSQL session."""
    assert is_postgres_session(pg_db) is True


# ---------------------------------------------------------------------------
# Test 2: the incidents table is fully created by the fixture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incidents_table_created_with_composite_indexes(
    pg_db: AsyncSession,
) -> None:
    """Base.metadata.create_all produces the incidents table and its btree indexes.

    The vector index is deliberately NOT asserted here: create_all cannot emit
    backend-specific DDL like CREATE INDEX USING hnsw, so this fixture's schema
    never has one. The vector index is owned by alembic and is covered by
    test_hnsw_index_created_by_migration below, which runs the real migration.
    """
    def _get_indexes(conn):
        inspector = inspect(conn)
        return [idx["name"] for idx in inspector.get_indexes("incidents")]

    async with pg_db.bind.connect() as conn:
        indexes = await conn.run_sync(_get_indexes)

    assert "ix_incidents_tenant_detected" in indexes
    assert "ix_incidents_resolved_detected" in indexes


# NOTE: that migration 023 leaves an HNSW index (and no ivfflat index) on the
# incidents table is asserted in .github/workflows/postgres-tests.yml, directly
# after `alembic upgrade head`. It cannot be checked here: the pg_db fixture
# runs Base.metadata.drop_all/create_all, which destroys the migrated schema, so
# any such test would pass or fail depending on execution order.


# ---------------------------------------------------------------------------
# Test 2c: HNSW search agrees with an exact scan (recall guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hnsw_recall_matches_exact_scan(pg_db: AsyncSession) -> None:
    """ANN results must match an exact scan for the configured ef_search.

    Guards the regression this migration fixes: the previous ivfflat index at
    pgvector's default probes=1 returned only ~70% of the true nearest
    neighbours. The failure mode is silent — wrong neighbours, no error — so
    ordering assertions alone do not catch it.

    Uses the fixture's create_all schema (no vector index), which makes this a
    correctness guard on find_similar_incidents itself; the indexed path is
    exercised by the same assertion in CI where the migration has run.
    """
    memory = IncidentMemory(pg_db)
    rng = np.random.default_rng(20260801)
    tenant = "recall_tenant"
    stored: List[List[float]] = []

    for _ in range(300):
        embedding = rng.normal(size=LATENT_DIM).tolist()
        stored.append(embedding)
        await memory.store_incident(
            tenant_id=tenant,
            embedding=embedding,
            anomaly_score=0.5,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
        )

    k = 10
    query = rng.normal(size=LATENT_DIM).tolist()
    query_np = np.asarray(query, dtype=np.float64)

    expected_distances = sorted(
        compute_cosine_distance(query_np, np.asarray(e, dtype=np.float64))
        for e in stored
    )[:k]

    results = await find_similar_incidents(
        pg_db, embedding=query, tenant_id=tenant, k=k
    )

    assert len(results) == k
    actual_distances = [distance for _, distance in results]
    for actual, expected in zip(actual_distances, expected_distances):
        assert actual == pytest.approx(expected, abs=1e-6), (
            "ANN search returned a different neighbour set than the exact scan: "
            f"got {actual_distances}, expected {expected_distances}"
        )


# ---------------------------------------------------------------------------
# Test 3: Vector cosine similarity ordering (exact values)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pgvector_cosine_similarity_ordering_exact_values(
    pg_db: AsyncSession,
) -> None:
    """Near embedding scores distance ≈ 0.0; opposite embedding scores ≈ 2.0."""
    memory = IncidentMemory(pg_db)
    query = unit_embedding(0)  # [1, 0, 0, ..., 0]
    opposite = unit_embedding(0, sign=-1.0)  # [-1, 0, 0, ..., 0]

    near_id = await memory.store_incident(
        tenant_id="tenant_pg",
        embedding=query,
        anomaly_score=0.8,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    far_id = await memory.store_incident(
        tenant_id="tenant_pg",
        embedding=opposite,
        anomaly_score=0.9,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )

    similar = await memory.find_similar(embedding=query, tenant_id="tenant_pg", k=2)

    assert len(similar) == 2
    first_incident, first_distance = similar[0]
    second_incident, second_distance = similar[1]

    assert first_incident.id == near_id
    assert first_distance == pytest.approx(0.0, abs=1e-5)
    assert second_incident.id == far_id
    assert second_distance == pytest.approx(2.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Test 4: find_similar returns results in distance order (nearest first)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_similar_returns_nearest_first(pg_db: AsyncSession) -> None:
    """pgvector cosine distance ordering: smaller distance returned first."""
    memory = IncidentMemory(pg_db)
    query = unit_embedding(0)

    embeddings = [
        unit_embedding(0, sign=-1.0),  # opposite  → distance ≈ 2.0
        unit_embedding(0),  # same      → distance ≈ 0.0
        unit_embedding(1),  # orthogonal → distance ≈ 1.0
    ]
    for emb in embeddings:
        await memory.store_incident(
            tenant_id="order_tenant",
            embedding=emb,
            anomaly_score=0.5,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
        )

    similar = await find_similar_incidents(
        pg_db, embedding=query, tenant_id="order_tenant", k=3
    )

    assert len(similar) == 3
    distances = [d for _, d in similar]
    assert distances == sorted(distances), "Distances must be in ascending order"
    assert distances[0] == pytest.approx(0.0, abs=1e-5)
    assert distances[1] == pytest.approx(1.0, abs=1e-5)
    assert distances[2] == pytest.approx(2.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Test 5: Tenant isolation — tenant B incidents not returned for tenant A
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_isolation_with_real_postgres(pg_db: AsyncSession) -> None:
    """Incidents from tenant B must not appear in tenant A results."""
    memory = IncidentMemory(pg_db)
    embedding = unit_embedding(2)

    await memory.store_incident(
        tenant_id="iso_tenant_a",
        embedding=embedding,
        anomaly_score=0.6,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )
    await memory.store_incident(
        tenant_id="iso_tenant_b",
        embedding=embedding,
        anomaly_score=0.6,
        sequence=sample_sequence(),
        embedding_uncertainty=0.1,
    )

    results_a = await find_similar_incidents(
        pg_db, embedding=embedding, tenant_id="iso_tenant_a", k=10
    )
    assert len(results_a) == 1
    assert results_a[0][0].tenant_id == "iso_tenant_a"

    results_b = await find_similar_incidents(
        pg_db, embedding=embedding, tenant_id="iso_tenant_b", k=10
    )
    assert len(results_b) == 1
    assert results_b[0][0].tenant_id == "iso_tenant_b"


# ---------------------------------------------------------------------------
# Test 6: pgvector distance matches numpy reference implementation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pgvector_distance_matches_numpy_reference(pg_db: AsyncSession) -> None:
    """pgvector <=> distance must equal the numpy cosine distance implementation."""
    memory = IncidentMemory(pg_db)
    query = unit_embedding(3)

    for axis in range(min(4, LATENT_DIM)):
        await memory.store_incident(
            tenant_id="ref_tenant",
            embedding=unit_embedding(axis),
            anomaly_score=0.5,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
        )

    similar = await find_similar_incidents(
        pg_db, embedding=query, tenant_id="ref_tenant", k=4
    )

    query_np = np.asarray(query, dtype=np.float64)
    for incident, pg_distance in similar:
        stored_np = np.asarray(incident.embedding, dtype=np.float64)
        numpy_distance = compute_cosine_distance(query_np, stored_np)
        assert pg_distance == pytest.approx(numpy_distance, abs=1e-5), (
            f"pgvector distance {pg_distance} != numpy distance {numpy_distance} "
            f"for embedding {incident.embedding}"
        )


# ---------------------------------------------------------------------------
# Test 7: k limit is respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_similar_respects_k_limit(pg_db: AsyncSession) -> None:
    """find_similar must return at most k results even when more exist."""
    memory = IncidentMemory(pg_db)
    embedding = unit_embedding(0)

    for i in range(5):
        await memory.store_incident(
            tenant_id="k_tenant",
            embedding=unit_embedding(i % LATENT_DIM),
            anomaly_score=0.5,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
        )

    results = await memory.find_similar(embedding=embedding, tenant_id="k_tenant", k=2)
    assert len(results) == 2


# ---------------------------------------------------------------------------
# Test 8: Multi-dimensional embedding search works across all latent axes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multidimensional_embedding_search(pg_db: AsyncSession) -> None:
    """pgvector search correctly identifies nearest incident across all axes."""
    memory = IncidentMemory(pg_db)

    # Store one unit vector per latent axis.
    for axis in range(LATENT_DIM):
        await memory.store_incident(
            tenant_id="multi_tenant",
            embedding=unit_embedding(axis),
            anomaly_score=0.5,
            sequence=sample_sequence(),
            embedding_uncertainty=0.1,
        )

    # Query along each axis; the matching incident must be returned first.
    for axis in range(LATENT_DIM):
        query = unit_embedding(axis)
        similar = await memory.find_similar(
            embedding=query, tenant_id="multi_tenant", k=1
        )
        assert len(similar) == 1
        nearest_incident, distance = similar[0]
        assert distance == pytest.approx(0.0, abs=1e-5), (
            f"Expected distance 0.0 querying axis {axis}, got {distance}"
        )
        assert nearest_incident.embedding[axis] == pytest.approx(1.0, abs=1e-5)

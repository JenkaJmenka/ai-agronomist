"""
Backend-portable vector similarity search over stored incidents.

Dual vector backend (user-approved architecture decision):
- PostgreSQL: native pgvector cosine distance (``<=>``), index-accelerated.
- SQLite (dev default + tests): embeddings live in a JSON column; cosine
  distance is computed in Python (numpy) over filtered candidate rows.

The backend is detected from the session's engine dialect so that
IncidentMemory callers never branch on it.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

import numpy as np
from sqlalchemy import ColumnElement, Float, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import HNSW_EF_SEARCH
from src.db.embedding_type import POSTGRES_DIALECT_NAME
from src.db.models import Incident

logger = logging.getLogger(__name__)

# Cap on candidate rows pulled into Python for the SQLite cosine fallback.
# Dev/test datasets are small; production runs on PostgreSQL/pgvector.
SQLITE_CANDIDATE_LIMIT: int = 5000

# Guard against division by zero for degenerate (all-zero) embeddings.
COSINE_NORM_EPSILON: float = 1e-12

# pgvector cosine-distance operator: returns 1 - cosine_similarity.
PGVECTOR_COSINE_OPERATOR: str = "<=>"

IncidentDistancePair = Tuple[Incident, float]


def is_postgres_session(session: AsyncSession) -> bool:
    """Detect whether the session is bound to a PostgreSQL engine."""
    bind = session.bind if session.bind is not None else session.get_bind()
    return bind.dialect.name == POSTGRES_DIALECT_NAME


def compute_cosine_distance(vector_a: np.ndarray, vector_b: np.ndarray) -> float:
    """Cosine distance (1 - cosine similarity), matching pgvector's ``<=>``."""
    norm_product = float(np.linalg.norm(vector_a) * np.linalg.norm(vector_b))
    if norm_product < COSINE_NORM_EPSILON:
        # Degenerate zero vector: treat as maximally dissimilar-but-orthogonal.
        return 1.0
    similarity = float(np.dot(vector_a, vector_b)) / norm_product
    return 1.0 - similarity


def build_incident_filters(
    tenant_id: str,
    greenhouse_id: Optional[str] = None,
    max_age_days: Optional[int] = None,
    resolved_only: bool = False,
) -> List[ColumnElement[bool]]:
    """Build the shared WHERE conditions for incident queries (tenant-scoped)."""
    conditions: List[ColumnElement[bool]] = [Incident.tenant_id == tenant_id]
    if greenhouse_id is not None:
        conditions.append(Incident.greenhouse_id == greenhouse_id)
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        conditions.append(Incident.detected_at >= cutoff)
    if resolved_only:
        conditions.append(Incident.resolved.is_(True))
    return conditions


async def find_similar_incidents(
    session: AsyncSession,
    *,
    embedding: Sequence[float],
    tenant_id: str,
    greenhouse_id: Optional[str] = None,
    k: int = 5,
    max_age_days: Optional[int] = None,
    resolved_only: bool = False,
) -> List[IncidentDistancePair]:
    """
    Return the k nearest incidents by cosine distance, most similar first,
    as (incident, distance) pairs. Always tenant-scoped.
    """
    query_embedding = [float(component) for component in embedding]
    conditions = build_incident_filters(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        max_age_days=max_age_days,
        resolved_only=resolved_only,
    )
    if is_postgres_session(session):
        return await _find_similar_pgvector(session, conditions, query_embedding, k)
    return await _find_similar_python(session, conditions, query_embedding, k)


async def _find_similar_pgvector(
    session: AsyncSession,
    conditions: List[ColumnElement[bool]],
    query_embedding: List[float],
    k: int,
) -> List[IncidentDistancePair]:
    """Native pgvector cosine-distance search (PostgreSQL only)."""
    # Lazy import: pgvector is only required on the PostgreSQL path.
    from pgvector.sqlalchemy import Vector

    # pgvector defaults hnsw.ef_search to 40, which measured 98.3% recall@10 on
    # 30k incidents; HNSW_EF_SEARCH restores parity with an exact scan. SET LOCAL
    # scopes the GUC to the surrounding transaction so it cannot leak across a
    # pooled connection. Interpolated, not bound: SET does not accept parameters.
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(HNSW_EF_SEARCH)}"))

    query_param = bindparam(
        "query_embedding", value=query_embedding, type_=Vector(len(query_embedding))
    )
    distance_column = (
        Incident.embedding.op(PGVECTOR_COSINE_OPERATOR, return_type=Float)(query_param)
    ).label("distance")
    statement = (
        select(Incident, distance_column)
        .where(*conditions)
        .order_by(distance_column)
        .limit(k)
    )
    rows = (await session.execute(statement)).all()
    return [(incident, float(distance)) for incident, distance in rows]


async def _find_similar_python(
    session: AsyncSession,
    conditions: List[ColumnElement[bool]],
    query_embedding: List[float],
    k: int,
) -> List[IncidentDistancePair]:
    """Numpy cosine-distance fallback over candidate rows (SQLite et al.)."""
    statement = select(Incident).where(*conditions).limit(SQLITE_CANDIDATE_LIMIT)
    candidates = (await session.execute(statement)).scalars().all()
    if len(candidates) == SQLITE_CANDIDATE_LIMIT:
        logger.warning(
            "SQLite vector search hit the %d-candidate cap; results may be "
            "approximate. Use PostgreSQL/pgvector for large datasets.",
            SQLITE_CANDIDATE_LIMIT,
        )
    query_vector = np.asarray(query_embedding, dtype=np.float64)
    scored = [
        (
            incident,
            compute_cosine_distance(
                query_vector, np.asarray(incident.embedding, dtype=np.float64)
            ),
        )
        for incident in candidates
    ]
    scored.sort(key=lambda pair: pair[1])
    return scored[:k]

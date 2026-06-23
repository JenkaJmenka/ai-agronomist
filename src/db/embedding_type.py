"""
Dialect-aware embedding column type for the dual vector backend.

On PostgreSQL the column compiles to a native pgvector ``Vector(dim)``
(enabling index-accelerated cosine distance). On every other dialect
(SQLite dev default and tests) the embedding is stored as JSON and
similarity is computed in Python — see src/db/vector_search.py.

Callers always read/write plain ``list[float]`` regardless of backend.
"""

from typing import Any, List, Optional

from sqlalchemy import JSON
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

POSTGRES_DIALECT_NAME = "postgresql"


class EmbeddingType(TypeDecorator):
    """Stores a fixed-dimension float vector portably across backends."""

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int) -> None:
        if dim < 1:
            raise ValueError(f"Embedding dimension must be >= 1, got {dim}")
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == POSTGRES_DIALECT_NAME:
            # Lazy import: pgvector is only required when running on PostgreSQL.
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(JSON())

    def process_bind_param(
        self, value: Optional[Any], dialect: Dialect
    ) -> Optional[List[float]]:
        if value is None:
            return None
        embedding = [float(component) for component in value]
        if len(embedding) != self.dim:
            raise ValueError(
                f"Embedding has {len(embedding)} dimensions, expected {self.dim}"
            )
        return embedding

    def process_result_value(
        self, value: Optional[Any], dialect: Dialect
    ) -> Optional[List[float]]:
        if value is None:
            return None
        # pgvector returns numpy arrays; JSON returns lists — normalize to list[float].
        return [float(component) for component in value]

"""Replace the ivfflat incident-embedding index with HNSW.

Revision ID: 023
Revises: 022
Create Date: 2026-08-01

Root cause
----------
Migration 002 creates ``ix_incidents_embedding_ivfflat`` in the *same* statement
block that creates the ``incidents`` table, i.e. against zero rows. Two problems
follow, and the second is the one that actually bites:

1. ivfflat derives its centroid lists by k-means at build time. Built on an empty
   table there is nothing to cluster, so the list structure never reflects the
   data that arrives later.
2. pgvector defaults ``ivfflat.probes`` to 1. With ``lists = 100`` a query
   examines roughly one hundredth of the candidate space. ``src/db/vector_search.py``
   never sets this GUC, so every production similarity query has run at probes=1.

The result is silent recall loss: ``find_similar_incidents`` returns plausible
but incomplete neighbour sets. Nothing errors, so this never surfaced.

Measured — 30k incidents, 3 tenants, LATENT_DIM=8, pgvector 0.8.2, PG15.
Recall@10 measured over 30 query vectors (300 ground-truth pairs) against an
exact scan with ANN indexes disabled:

    ivfflat as built by 002 (empty table), probes=1 (default)  ......  70.0 %
    ivfflat as built by 002, probes=10 .............................. 100.0 %
    ivfflat REINDEXed on the populated table, probes=1 ..............  56.7 %
    ivfflat REINDEXed on the populated table, probes=10 .............  99.7 %
    HNSW m=16 ef_construction=64, ef_search=40 (default) ............  98.3 %
    HNSW m=16 ef_construction=64, ef_search=100 ..................... 100.0 %

Note the counter-intuitive row: REINDEXing the ivfflat index on populated data
makes recall *worse* at default probes (70.0 % -> 56.7 %), because a properly
clustered index concentrates candidates into lists that a single probe then
misses. "Just reindex it" is therefore not a fix.

HNSW is chosen because it is correct at stock settings: it needs no build-time
training data and no GUC tuning to reach ~98 %, and reaches 100 % at
ef_search=100 (set by src/config.py:HNSW_EF_SEARCH via vector_search.py).

Cost: the HNSW index is larger (~9.8 MB vs ~1 MB for ivfflat at 30k rows) and
slower to build (~2.3 s at 30k rows). See "Deploy" below.

Deploy
------
docker/start-api.sh runs ``alembic upgrade head`` on API boot, so this index is
built during deploy while writes are blocked. At current incident volumes the
build is seconds. If the table grows past ~1M rows, move this build to a one-off
ops step using CREATE INDEX CONCURRENTLY, which cannot run inside Alembic's
transaction.

pgvector >= 0.5.0 is required for HNSW; the guard below fails loudly rather than
letting CREATE INDEX emit a cryptic syntax error.
"""

from alembic import op
import sqlalchemy as sa

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None

POSTGRES_DIALECT_NAME = "postgresql"

# HNSW build parameters. m = edges per node; ef_construction = candidate list
# size while building. 16/64 are the pgvector defaults and measured 98.3 %
# recall@10 on this dataset; raising them costs build time and index size.
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64

# Restored verbatim by downgrade() — matches IVFFLAT_LISTS in migration 002.
IVFFLAT_LISTS = 100

# HNSW support landed in pgvector 0.5.0.
MIN_PGVECTOR_VERSION_FOR_HNSW = (0, 5, 0)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == POSTGRES_DIALECT_NAME


def _require_hnsw_support() -> None:
    """Fail with an actionable message when the server's pgvector predates HNSW."""
    raw_version = op.get_bind().execute(
        sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).scalar()
    if raw_version is None:
        raise RuntimeError(
            "The 'vector' extension is not installed, but migration 002 requires "
            "it. Run: CREATE EXTENSION vector;"
        )
    # extversion looks like '0.8.2'. Compare the full tuple, not the trailing
    # components: splitting off the leading segment would misread a future 1.0.0
    # as older than 0.5.0. Unparseable versions fall through — CREATE INDEX will
    # fail with its own error rather than blocking a deploy on a cosmetic suffix.
    try:
        parts = tuple(int(part) for part in raw_version.split(".")[:3])
    except ValueError:
        return
    if parts < MIN_PGVECTOR_VERSION_FOR_HNSW:
        raise RuntimeError(
            f"pgvector {raw_version} does not support HNSW indexes "
            f"(requires >= 0.5.0). Upgrade the pgvector extension on this server, "
            f"then re-run this migration."
        )


def upgrade() -> None:
    if not _is_postgres():
        # SQLite stores embeddings as JSON and searches them in Python
        # (src/db/vector_search.py::_find_similar_python) — no index applies.
        return

    _require_hnsw_support()

    # Create before dropping so a failed build leaves the old index in place
    # even if this runs outside a transaction.
    op.create_index(
        "ix_incidents_embedding_hnsw",
        "incidents",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": HNSW_M, "ef_construction": HNSW_EF_CONSTRUCTION},
    )
    op.drop_index("ix_incidents_embedding_ivfflat", table_name="incidents")


def downgrade() -> None:
    if not _is_postgres():
        return

    op.create_index(
        "ix_incidents_embedding_ivfflat",
        "incidents",
        ["embedding"],
        postgresql_using="ivfflat",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"lists": IVFFLAT_LISTS},
    )
    op.drop_index("ix_incidents_embedding_hnsw", table_name="incidents")

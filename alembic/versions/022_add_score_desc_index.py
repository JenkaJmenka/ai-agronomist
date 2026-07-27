"""Add anomaly_score DESC index to serve ORDER BY score DESC LIMIT N efficiently.

Revision ID: 022
Revises: 021
Create Date: 2026-07-28

Root cause
----------
get_enriched_anomalies() calls fetch_top_flagged_rows_by_score() which runs:

    SELECT * FROM sensor_readings
    WHERE tenant_id = $1 AND greenhouse_id = $2
      AND timestamp >= $3 AND timestamp <= $4
      AND anomaly_score >= $5
    ORDER BY anomaly_score DESC
    LIMIT 50

At FULL RANGE, the timestamp predicate is very wide (a year of data) so the
existing (tenant_id, greenhouse_id, timestamp, anomaly_score) covering index
from 021 cannot provide a useful ORDER BY: Postgres falls back to a sequential
scan over the entire flagged set, then a top-N heapsort.

EXPLAIN (ANALYZE, BUFFERS) — before, 35k rows, Postgres 16.14 (local):
  Seq Scan on sensor_readings
    Filter: tenant_id + greenhouse_id + timestamp range + anomaly_score >= 0.5
    Rows Removed by Filter: 30659
  Sort Key: anomaly_score DESC (top-N heapsort, 4381 candidate rows)
  Execution Time: 10.570 ms  (local; ~9.8 s in production with larger dataset)

EXPLAIN (ANALYZE, BUFFERS) — after this migration, same data:
  Index Scan using ix_sensor_readings_score_desc on sensor_readings
    Index Cond: tenant_id = 'demo' AND greenhouse_id = 'default'
                AND anomaly_score >= 0.5
    Filter: timestamp range (passes almost all rows at FULL RANGE)
    Rows Removed by Filter: 3
  Execution Time: 0.277 ms  (38x speedup local; ratio grows with dataset size)

For narrow windows (1d/7d/30d) the planner correctly continues to prefer the
(tenant_id, greenhouse_id, timestamp, anomaly_score) index from 021 because
the timestamp predicate is highly selective there — no regression.

Partial index
-------------
WHERE anomaly_score IS NOT NULL keeps the index small: un-scored live-stream
rows never appear in flagged-row queries and don't need to occupy index pages.

Compatibility
-------------
CREATE INDEX IF NOT EXISTS is valid on SQLite 3.25+ and PostgreSQL 9.5+.
Partial indexes (WHERE clause) are supported on both.
"""

from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Partial index on (tenant_id, greenhouse_id, anomaly_score DESC):
    # - Allows ORDER BY anomaly_score DESC LIMIT N to be served by an index
    #   walk without a sort pass over all filtered rows.
    # - DESC ordering is stored so Postgres can walk high→low without reversal.
    # - WHERE anomaly_score IS NOT NULL excludes un-scored rows from the index.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sensor_readings_score_desc "
        "ON sensor_readings (tenant_id, greenhouse_id, anomaly_score DESC) "
        "WHERE anomaly_score IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sensor_readings_score_desc")

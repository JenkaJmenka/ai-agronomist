"""Add covering index for anomaly group queries.

Revision ID: 021
Revises: 020
Create Date: 2026-07-27

Adds (tenant_id, greenhouse_id, timestamp, anomaly_score) covering index on
sensor_readings. This index handles the WHERE clause used by both endpoints:

    WHERE tenant_id=? AND greenhouse_id=? AND timestamp BETWEEN ? AND ?
      AND anomaly_score >= ?

The dead-branch migration 419da3d53b22 added ix_sensor_readings_tenant_gh_time
(tenant_id, greenhouse_id, timestamp) against chain 015 and was never merged
into the main chain. This migration adds that index too via CREATE INDEX IF
NOT EXISTS so production instances that never ran the dead-branch migration
get it, while those that did are unaffected.

Compatibility
-------------
CREATE INDEX IF NOT EXISTS is valid SQL on SQLite 3.25+ and PostgreSQL 9.5+.
Both variants are used in production.
"""

from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Base composite index — covers the time-range portion of the anomaly filter.
    # IF NOT EXISTS protects against dead-branch 419da3d53b22 already being applied.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sensor_readings_tenant_gh_time "
        "ON sensor_readings (tenant_id, greenhouse_id, timestamp)"
    )
    # Covering index that includes anomaly_score so the score threshold filter
    # can be satisfied without a table row fetch. Allows SQL GROUP BY date +
    # COUNT(*) / MAX(anomaly_score) to run as a pure index scan.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sensor_readings_anomaly_covering "
        "ON sensor_readings (tenant_id, greenhouse_id, timestamp, anomaly_score)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sensor_readings_anomaly_covering")
    # Leave ix_sensor_readings_tenant_gh_time — may have originated from the
    # dead-branch migration that we must not unilaterally drop.

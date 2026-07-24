"""Add source column to sensor_readings; backfill live provenance marker.

Revision ID: 017
Revises: 016
Create Date: 2026-07-24

Adds a nullable ``source`` String(64) column to ``sensor_readings``. The
column carries row-provenance information (e.g. ``'live'`` for rows persisted
by the live SCADA pipeline) without overloading the domain column
``anomaly_type``, which is reserved for the inject-test subsystem.

Backfill
--------
Rows written by the live pipeline before this migration used
``anomaly_type='live'`` as a provenance sentinel.  The backfill moves that
marker to the new ``source`` column and clears ``anomaly_type`` so the domain
column is no longer semantically overloaded.

  UPDATE sensor_readings
     SET source='live', anomaly_type=NULL
   WHERE anomaly_type='live'

Downgrade reverses the backfill and drops the column.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite does not support ALTER TABLE ADD COLUMN with non-trivial defaults;
    # batch mode recreates the table with the new column inline (SQLite compat,
    # mirrors migration 011 approach).
    with op.batch_alter_table("sensor_readings") as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(64), nullable=True))

    # Backfill: move the 'live' provenance marker from the domain column
    # (anomaly_type) to the new source column, then clear anomaly_type so it
    # is no longer semantically overloaded.
    op.execute(
        "UPDATE sensor_readings SET source='live', anomaly_type=NULL"
        " WHERE anomaly_type='live'"
    )


def downgrade() -> None:
    # Reverse backfill: restore anomaly_type='live' for all live-source rows,
    # then drop the column.
    op.execute(
        "UPDATE sensor_readings SET anomaly_type='live', source=NULL"
        " WHERE source='live'"
    )
    with op.batch_alter_table("sensor_readings") as batch_op:
        batch_op.drop_column("source")

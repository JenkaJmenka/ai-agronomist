"""Add trigger label columns for alert integrity (ADR-006).

Revision ID: 024
Revises: 023
Create Date: 2026-09-08

Background
----------
The CSV upload path persists the triggering column name, detector identity,
severity, and raw sensor value alongside each anomaly score row so that the
group/enriched API endpoints can report what actually fired rather than
inferring it from four stored canonical sensor values.

Four defects (D1–D4) addressed in this migration:

D1 — trigger_channel / trigger_detector: the channel with the highest |z|
     at each flagged timestamp and the detector that produced the flag are
     now persisted directly on SensorReading.  Previously they were written
     only to RawReading.data and never read back by the API.

D3 — The statistical score was saturated (every hit scored exactly 1.0
     because z_score / 5.0 >= 1.1 for all surviving z >= 5.5 hits).  The
     fix is in upload_portions.py; this migration adds the columns needed to
     surface the corrected scores.

D4 — detected_severity from the detector (critical / warning) is now
     persisted so the frontend can show it without re-computing from score.

D5 — trigger_value stores the raw sensor value at trigger_channel so the
     reasoning text can quote it for non-canonical channels (pipe temps etc.)
     that are not stored in the four canonical SensorReading columns.

New columns
-----------
* ``trigger_channel   VARCHAR(255) NULL`` — raw detector column name
* ``trigger_detector  VARCHAR(64)  NULL`` — detector producing the hit
* ``detected_severity VARCHAR(16)  NULL`` — critical | warning | None
* ``trigger_value     FLOAT        NULL`` — sensor value at trigger_channel

All columns are nullable with no default → metadata-only DDL on Postgres,
no table rewrite, no long lock.  NULL means the row predates this migration
or was scored by the LSTM-only path (no per-column attribution).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sensor_readings",
        sa.Column("trigger_channel", sa.String(255), nullable=True),
    )
    op.add_column(
        "sensor_readings",
        sa.Column("trigger_detector", sa.String(64), nullable=True),
    )
    op.add_column(
        "sensor_readings",
        sa.Column("detected_severity", sa.String(16), nullable=True),
    )
    op.add_column(
        "sensor_readings",
        sa.Column("trigger_value", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sensor_readings", "trigger_value")
    op.drop_column("sensor_readings", "detected_severity")
    op.drop_column("sensor_readings", "trigger_detector")
    op.drop_column("sensor_readings", "trigger_channel")

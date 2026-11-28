"""ADR-014 A8: persist the upload fault label on sensor_readings.

Revision ID: 032
Revises: 031
Create Date: 2026-11-28

- sensor_readings.fault_type String(64), nullable. Written by the upload path
  as legacy_upload_fault_label(trigger_detector) for every flagged row. NULL
  for rows written earlier; readers fall through to the trigger_detector map
  and then value inference, so those rows keep the label they have today.
"""

from alembic import op
import sqlalchemy as sa

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sensor_readings") as batch:
        batch.add_column(sa.Column("fault_type", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sensor_readings") as batch:
        batch.drop_column("fault_type")

"""Add anomaly_type column to sensor_readings.

Revision ID: 009
Revises: 008
Create Date: 2026-06-20

"""
from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sensor_readings",
        sa.Column("anomaly_type", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sensor_readings", "anomaly_type")

"""Add raw_readings table.

Revision ID: 008
Revises: 007
Create Date: 2026-06-19

"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "raw_readings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("greenhouse_id", sa.String(255), nullable=False, server_default="default"),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("(CURRENT_TIMESTAMP)")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_raw_readings_tenant_id", "raw_readings", ["tenant_id"])
    op.create_index("ix_raw_readings_timestamp", "raw_readings", ["timestamp"])
    op.create_index("ix_raw_readings_tenant_timestamp", "raw_readings", ["tenant_id", "timestamp"])


def downgrade() -> None:
    op.drop_index("ix_raw_readings_tenant_timestamp", table_name="raw_readings")
    op.drop_index("ix_raw_readings_timestamp", table_name="raw_readings")
    op.drop_index("ix_raw_readings_tenant_id", table_name="raw_readings")
    op.drop_table("raw_readings")

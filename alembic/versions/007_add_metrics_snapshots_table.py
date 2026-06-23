"""Add metrics_snapshots table.

Revision ID: 007
Revises: 006
Create Date: 2026-06-16

"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metrics_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_detections", sa.Integer(), nullable=False, default=0),
        sa.Column("false_positive_rate", sa.Float(), nullable=False, default=0.0),
        sa.Column("mean_confidence", sa.Float(), nullable=False, default=0.0),
        sa.Column("pattern_distribution", sa.JSON(), nullable=False),
        sa.Column("confidence_distribution", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_metrics_snapshots_tenant_id", "metrics_snapshots", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_metrics_snapshots_tenant_id", table_name="metrics_snapshots")
    op.drop_table("metrics_snapshots")

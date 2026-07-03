"""Add model_artifacts table (cross-service checkpoint store).

Revision ID: 015
Revises: 014
Create Date: 2026-07-03

"""
import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("key", sa.String(length=512), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("key", name="uq_model_artifacts_key"),
    )
    op.create_index("ix_model_artifacts_key", "model_artifacts", ["key"])


def downgrade() -> None:
    op.drop_index("ix_model_artifacts_key", table_name="model_artifacts")
    op.drop_table("model_artifacts")

"""Add customer_model_config table for per-tenant tuning overrides.

Revision ID: 006
Revises: 005
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa


revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_model_config",
        sa.Column("customer_id", sa.String(length=255), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("customer_id"),
    )


def downgrade() -> None:
    op.drop_table("customer_model_config")

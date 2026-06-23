"""Add learning_states table for persistent active-learning orchestrators.

Revision ID: 005
Revises: 004
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa


revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_states",
        sa.Column("customer_id", sa.String(length=255), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("customer_id"),
    )


def downgrade() -> None:
    op.drop_table("learning_states")

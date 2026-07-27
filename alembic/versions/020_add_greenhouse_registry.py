"""Add greenhouse registry table.

Revision ID: 020
Revises: 019
Create Date: 2026-07-27

Table: greenhouses
  Composite primary key: (tenant_id, greenhouse_id).
  Decouples greenhouse existence from sensor readings so tenants can be
  provisioned with zero readings (e.g. demo tenants) and still get a
  non-empty greenhouse selector and meaningful live-status responses.

Compatibility
-------------
Works on both SQLite and PostgreSQL:
  - sa.DateTime(timezone=True) → TEXT on SQLite, TIMESTAMPTZ on PostgreSQL.
  - sa.text("CURRENT_TIMESTAMP") is valid SQL on both dialects.
  - render_as_batch=True is set in alembic/env.py (required for SQLite ALTER).
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "greenhouses",
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("greenhouse_id", sa.String(255), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "greenhouse_id", name="pk_greenhouses"),
    )


def downgrade() -> None:
    op.drop_table("greenhouses")

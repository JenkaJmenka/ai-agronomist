"""Add operational_config table for tenant/greenhouse-scoped SCADA settings.

NOTE: src/db/migrations/ is dead — alembic/ is the authoritative migration tree
(per alembic.ini).  All new migrations belong here, not in src/db/migrations/.

Revision ID: 018
Revises: 017
Create Date: 2026-07-24

Table: operational_config
  Composite primary key: (tenant_id, greenhouse_id).
  config_json: full SCADA operational document — crop, setpoints, day_window,
               season_map, seasonal_overrides.
  updated_at: timestamp with server default CURRENT_TIMESTAMP (now() on Postgres).

Compatibility
-------------
Works on both SQLite and PostgreSQL:
  - sa.JSON() maps to TEXT on SQLite and JSONB/JSON on PostgreSQL.
  - sa.text("CURRENT_TIMESTAMP") is valid SQL on both dialects.
  - render_as_batch=True is set in alembic/env.py (required for SQLite ALTER).
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operational_config",
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column(
            "greenhouse_id",
            sa.String(255),
            nullable=False,
            server_default="default",
        ),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "greenhouse_id"),
    )


def downgrade() -> None:
    op.drop_table("operational_config")

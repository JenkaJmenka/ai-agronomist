"""Add live_scope_state table — persisted Go Live desired state.

Revision ID: 028
Revises: 027
Create Date: 2026-10-16

Table: live_scope_state
  Composite primary key: (tenant_id, greenhouse_id).
  Records the operator's **desired** state for a live scope ('live' or
  'paused') so that an API restart can resume the scopes the operator had
  turned on.  Before this table the only record of "Go Live was pressed"
  lived in the uvicorn process's memory (LiveRuntime task map +
  src/live/scope_control._suppressed_scopes), so every redeploy returned
  every scope to idle with no resume and no signal.

No backfill
-----------
This migration deliberately inserts **no rows**.  An empty table means
"every scope is idle", which is exactly the LIVE_AUTOSTART_ENABLED=false
semantics the application already has: connectors must never run for a
scope nobody turned on.  Backfilling 'live' for the configured
MOCK_SCADA_SCOPES would start connectors on the next boot for scopes no
operator had asked for — the regression commit 9c14dc3 was written to
prevent.  Operators press Go Live once after this ships.

Compatibility
-------------
Works on both SQLite and PostgreSQL:
  - sa.DateTime(timezone=True) -> TEXT on SQLite, TIMESTAMPTZ on PostgreSQL.
  - sa.text("CURRENT_TIMESTAMP") is valid SQL on both dialects.
  - The CheckConstraint is inline in CREATE TABLE, so no SQLite ALTER
    (and therefore no batch mode) is required.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_scope_state",
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("greenhouse_id", sa.String(255), nullable=False),
        sa.Column("desired_state", sa.String(16), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "greenhouse_id", name="pk_live_scope_state"
        ),
        sa.CheckConstraint(
            "desired_state IN ('live', 'paused')",
            name="ck_live_scope_state_desired_state",
        ),
    )


def downgrade() -> None:
    op.drop_table("live_scope_state")

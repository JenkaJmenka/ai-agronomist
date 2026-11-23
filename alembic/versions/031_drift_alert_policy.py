"""ADR-013 drift-watch alert policy: alert_state, robust offset, feedback.

Revision ID: 031
Revises: 030
Create Date: 2026-11-23

- drift_watch_inputs: alert_state (NOT NULL, default 'off'), alert_state_reason,
  alert_state_changed_at.
- drift_watch_episodes: offset_robust, suppressed_by.
- drift_watch_feedback: operator labels keyed on the episode natural key
  (tenant, greenhouse, input, direction, first_detected_ts). No foreign key to
  drift_watch_episodes on purpose: backfill recreates those rows and a label
  must survive it.
"""

from alembic import op
import sqlalchemy as sa

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("drift_watch_inputs") as batch:
        batch.add_column(
            sa.Column("alert_state", sa.String(16), nullable=False, server_default="off")
        )
        batch.add_column(sa.Column("alert_state_reason", sa.Text, nullable=True))
        batch.add_column(
            sa.Column("alert_state_changed_at", sa.DateTime(timezone=True), nullable=True)
        )
    with op.batch_alter_table("drift_watch_episodes") as batch:
        batch.add_column(sa.Column("offset_robust", sa.Float, nullable=True))
        batch.add_column(sa.Column("suppressed_by", sa.String(64), nullable=True))

    op.create_table(
        "drift_watch_feedback",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("greenhouse_id", sa.String(255), nullable=False, server_default="default"),
        sa.Column("input_id", sa.String(255), nullable=False),
        sa.Column("direction", sa.String(4), nullable=False),
        sa.Column("first_detected_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "greenhouse_id",
            "input_id",
            "direction",
            "first_detected_ts",
            name="uq_drift_watch_feedback_natural_key",
        ),
        sa.CheckConstraint(
            "label IN ('confirmed_drift', 'false_alarm')",
            name="ck_drift_watch_feedback_label",
        ),
    )


def downgrade() -> None:
    op.drop_table("drift_watch_feedback")
    with op.batch_alter_table("drift_watch_episodes") as batch:
        batch.drop_column("suppressed_by")
        batch.drop_column("offset_robust")
    with op.batch_alter_table("drift_watch_inputs") as batch:
        batch.drop_column("alert_state_changed_at")
        batch.drop_column("alert_state_reason")
        batch.drop_column("alert_state")

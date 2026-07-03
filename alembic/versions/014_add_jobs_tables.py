"""Add jobs and job_portions tables for queue-processed async work.

Revision ID: 014
Revises: 013
Create Date: 2026-07-03

"""
import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("greenhouse_id", sa.String(length=255), nullable=True),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("total_portions", sa.Integer(), nullable=True),
        sa.Column("completed_portions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_portions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(length=2000), nullable=True),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_tenant_created", "jobs", ["tenant_id", "created_at"])

    op.create_table(
        "job_portions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("portion_idx", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("job_id", "portion_idx", name="uq_job_portions_job_idx"),
    )
    op.create_index("ix_job_portions_job_id", "job_portions", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_job_portions_job_id", table_name="job_portions")
    op.drop_table("job_portions")
    op.drop_index("ix_jobs_tenant_created", table_name="jobs")
    op.drop_index("ix_jobs_tenant_id", table_name="jobs")
    op.drop_table("jobs")

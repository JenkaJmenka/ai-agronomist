"""Add model registry tables (model_baseline, model_version, model_deployment)

These tables were defined in src/db/models.py since Week 1-2 but never
covered by a migration — /api/inference fails on a migrations-only database
because ModelRegistry queries model_deployment. Plain portable DDL: works
identically on SQLite and PostgreSQL.

Revision ID: 004
Revises: 003
Create Date: 2026-06-11

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'model_baseline',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('customer_id', sa.String(), nullable=False),
        sa.Column('greenhouse_type', sa.String(), nullable=True),
        sa.Column('baseline_json', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'model_version',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('customer_id', sa.String(), nullable=False),
        sa.Column('model_type', sa.String(), nullable=False),
        sa.Column('checkpoint_path', sa.String(), nullable=False),
        sa.Column('baseline_id', sa.String(), nullable=True),
        sa.Column('metrics_json', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('status', sa.String(), nullable=False,
                  server_default='active'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'model_deployment',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('customer_id', sa.String(), nullable=False),
        sa.Column('model_id', sa.String(), nullable=False),
        sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('is_current', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('model_deployment')
    op.drop_table('model_version')
    op.drop_table('model_baseline')

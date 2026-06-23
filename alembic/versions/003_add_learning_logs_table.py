"""Add learning_logs table (Gap 3 — online learning audit trail)

Plain portable DDL: no vector columns, works identically on SQLite and
PostgreSQL.

Revision ID: 003
Revises: 002
Create Date: 2026-06-11

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'learning_logs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=255), nullable=False),
        sa.Column('greenhouse_id', sa.String(length=255), nullable=False,
                  server_default='default'),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('sequences_observed', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('updates_performed', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('loss_before', sa.Float(), nullable=True),
        sa.Column('loss_after', sa.Float(), nullable=True),
        sa.Column('drift_score', sa.Float(), nullable=True),
        sa.Column('drift_detected', sa.Boolean(), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('model_checkpoint_path', sa.String(length=512), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_learning_logs_tenant_id'), 'learning_logs', ['tenant_id'])
    op.create_index(op.f('ix_learning_logs_event_type'), 'learning_logs', ['event_type'])
    op.create_index('ix_learning_logs_tenant_created', 'learning_logs',
                    ['tenant_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_learning_logs_tenant_created', table_name='learning_logs')
    op.drop_index(op.f('ix_learning_logs_event_type'), table_name='learning_logs')
    op.drop_index(op.f('ix_learning_logs_tenant_id'), table_name='learning_logs')
    op.drop_table('learning_logs')

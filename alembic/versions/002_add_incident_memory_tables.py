"""Add incident memory tables (incidents, incident_actions)

Dual vector backend: on PostgreSQL the embedding column is a native
pgvector Vector and gets an ivfflat cosine index; on SQLite (dev/tests)
the embedding is stored as JSON and similarity is computed in Python.

Revision ID: 002
Revises: 001_initial
Create Date: 2026-06-11

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '002'
down_revision = '001_initial'
branch_labels = None
depends_on = None

# Frozen at migration time; matches src.config.LATENT_DIM when this revision
# was written. Migrations are immutable snapshots — do NOT import config here.
EMBEDDING_DIM = 8

# ivfflat tuning: number of inverted lists (pgvector default guidance for
# datasets up to ~100K rows).
IVFFLAT_LISTS = 100

POSTGRES_DIALECT_NAME = 'postgresql'


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == POSTGRES_DIALECT_NAME


def _embedding_column_type() -> sa.types.TypeEngine:
    if _is_postgres():
        # Lazy import: pgvector only required on the PostgreSQL path.
        from pgvector.sqlalchemy import Vector
        return Vector(EMBEDDING_DIM)
    return sa.JSON()


def upgrade() -> None:
    if _is_postgres():
        op.execute('CREATE EXTENSION IF NOT EXISTS vector')

    op.create_table(
        'incidents',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=255), nullable=False),
        sa.Column('greenhouse_id', sa.String(length=255), nullable=False,
                  server_default='default'),
        sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reported_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sequence', sa.JSON(), nullable=False),
        sa.Column('sequence_length', sa.Integer(), nullable=False),
        sa.Column('anomaly_score', sa.Float(), nullable=False),
        sa.Column('embedding', _embedding_column_type(), nullable=False),
        sa.Column('embedding_uncertainty', sa.Float(), nullable=False),
        sa.Column('root_cause', sa.String(length=256), nullable=True),
        sa.Column('root_cause_confidence', sa.Float(), nullable=True),
        sa.Column('root_cause_details', sa.JSON(), nullable=True),
        sa.Column('resolved', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('actions_taken', sa.JSON(), nullable=True),
        sa.Column('actions_effective', sa.Boolean(), nullable=True),
        sa.Column('effectiveness_score', sa.Float(), nullable=True),
        sa.Column('time_to_detect', sa.Integer(), nullable=True),
        sa.Column('time_to_resolve', sa.Integer(), nullable=True),
        sa.Column('crop_damage_estimated', sa.Float(), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('notes', sa.String(length=1024), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_incidents_tenant_id'), 'incidents', ['tenant_id'])
    op.create_index(op.f('ix_incidents_detected_at'), 'incidents', ['detected_at'])
    op.create_index(op.f('ix_incidents_root_cause'), 'incidents', ['root_cause'])
    op.create_index('ix_incidents_tenant_detected', 'incidents',
                    ['tenant_id', 'detected_at'])
    op.create_index('ix_incidents_resolved_detected', 'incidents',
                    ['resolved', 'detected_at'])

    if _is_postgres():
        # Approximate-nearest-neighbour index for cosine distance (<=>).
        op.create_index(
            'ix_incidents_embedding_ivfflat',
            'incidents',
            ['embedding'],
            postgresql_using='ivfflat',
            postgresql_ops={'embedding': 'vector_cosine_ops'},
            postgresql_with={'lists': IVFFLAT_LISTS},
        )

    op.create_table(
        'incident_actions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('incident_id', sa.String(length=36), nullable=False),
        sa.Column('action', sa.String(length=256), nullable=False),
        sa.Column('action_time', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('effectiveness', sa.Boolean(), nullable=True),
        sa.Column('notes', sa.String(length=512), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id']),
    )
    op.create_index(op.f('ix_incident_actions_incident_id'),
                    'incident_actions', ['incident_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_incident_actions_incident_id'), table_name='incident_actions')
    op.drop_table('incident_actions')
    if _is_postgres():
        op.drop_index('ix_incidents_embedding_ivfflat', table_name='incidents')
    op.drop_index('ix_incidents_resolved_detected', table_name='incidents')
    op.drop_index('ix_incidents_tenant_detected', table_name='incidents')
    op.drop_index(op.f('ix_incidents_root_cause'), table_name='incidents')
    op.drop_index(op.f('ix_incidents_detected_at'), table_name='incidents')
    op.drop_index(op.f('ix_incidents_tenant_id'), table_name='incidents')
    op.drop_table('incidents')
    # The vector extension is intentionally left installed (shared resource).

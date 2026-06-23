"""Add CHECK constraint on incidents.fault_type.

Revision ID: 011
Revises: 010
Create Date: 2026-06-21

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '011'
down_revision = '010'
branch_labels = None
depends_on = None

_VALID_VALUES = (
    'calibration_drift',
    'bias_offset',
    'stuck_fault',
    'accuracy_decline',
    'spike_fault',
    'unknown',
)

_CHECK_EXPR = "fault_type IS NULL OR fault_type IN ({})".format(
    ", ".join(f"'{v}'" for v in _VALID_VALUES)
)


def upgrade() -> None:
    # SQLite does not support ALTER TABLE ADD CONSTRAINT; use batch mode so
    # Alembic recreates the table with the new CHECK constraint inline.
    with op.batch_alter_table('incidents') as batch_op:
        batch_op.create_check_constraint(
            'ck_incidents_fault_type',
            _CHECK_EXPR,
        )


def downgrade() -> None:
    with op.batch_alter_table('incidents') as batch_op:
        batch_op.drop_constraint('ck_incidents_fault_type', type_='check')

"""Add polling_dropout to fault_type CHECK constraint.

Revision ID: 012
Revises: 011
Create Date: 2026-06-22

Extends the CHECK constraint on incidents.fault_type to include the new
'polling_dropout' fault category introduced by the SCADA Phase-A dropout
detector.  The downgrade restores the previous constraint (pre-012 values).
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_incidents_fault_type"
_TABLE_NAME = "incidents"

_VALUES_V2 = (
    "calibration_drift",
    "bias_offset",
    "stuck_fault",
    "accuracy_decline",
    "spike_fault",
    "unknown",
    "polling_dropout",
)

_VALUES_V1 = (
    "calibration_drift",
    "bias_offset",
    "stuck_fault",
    "accuracy_decline",
    "spike_fault",
    "unknown",
)


def _check_expr(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"fault_type IS NULL OR fault_type IN ({quoted})"


def upgrade() -> None:
    # SQLite requires batch mode (table-copy strategy) to drop/add constraints.
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            _check_expr(_VALUES_V2),
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            _check_expr(_VALUES_V1),
        )

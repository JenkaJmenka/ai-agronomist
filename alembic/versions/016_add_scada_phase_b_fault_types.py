"""Add Phase B SCADA fault types to CHECK constraint.

Revision ID: 016
Revises: 015
Create Date: 2026-07-09

Extends the CHECK constraint on incidents.fault_type to include the new
Phase B fault categories: out_of_bounds, correlation_breakdown,
diurnal_deviation, thermodynamic_contradiction.
"""
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_incidents_fault_type"
_TABLE_NAME = "incidents"

_VALUES_V3 = (
    "calibration_drift",
    "bias_offset",
    "stuck_fault",
    "accuracy_decline",
    "spike_fault",
    "unknown",
    "polling_dropout",
    "out_of_bounds",
    "correlation_breakdown",
    "diurnal_deviation",
    "thermodynamic_contradiction",
)

_VALUES_V2 = (
    "calibration_drift",
    "bias_offset",
    "stuck_fault",
    "accuracy_decline",
    "spike_fault",
    "unknown",
    "polling_dropout",
)


def _check_expr(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"fault_type IS NULL OR fault_type IN ({quoted})"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            _check_expr(_VALUES_V3),
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            _check_expr(_VALUES_V2),
        )

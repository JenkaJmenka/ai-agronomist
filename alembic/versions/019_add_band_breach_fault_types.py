"""Add band-breach + SCADA Phase A fault types to incidents CHECK constraint.

Revision ID: 019
Revises: 018
Create Date: 2026-07-24

Extends ``ck_incidents_fault_type`` with operator crop-band breach labels and
the Phase A SCADA heating/humidity types already emitted by the pipeline.
"""

from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_incidents_fault_type"
_TABLE_NAME = "incidents"

_VALUES_V4 = (
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
    "under_heating",
    "over_heating",
    "dehumidification_failure",
    "humidity_saturation_risk",
    "thermal_divergence",
    "temp_band_high",
    "temp_band_low",
    "vpd_band_high",
    "vpd_band_low",
    "humidity_band_high",
    "humidity_band_low",
    "co2_band_high",
    "co2_band_low",
)

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


def _check_expr(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"fault_type IS NULL OR fault_type IN ({quoted})"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            _check_expr(_VALUES_V4),
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            _check_expr(_VALUES_V3),
        )

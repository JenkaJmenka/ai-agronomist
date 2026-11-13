"""Allow humidity_fault on incidents.fault_type (BACKLOG-20).

Revision ID: 029
Revises: 028
Create Date: 2026-11-12

``psychrometric_inconsistency`` is mapped to ``humidity_fault`` in the
comprehensive pipeline. That label was missing from
``ck_incidents_fault_type`` (``_VALUES_V4`` in migration 019), so a
confirmed psychrometric incident failed the CHECK. ``out_of_bounds`` is
already in the constraint; this revision only adds ``humidity_fault``.

Downgrade fails if any row already stores ``humidity_fault``.
"""

from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None

_CONSTRAINT_NAME = "ck_incidents_fault_type"
_TABLE_NAME = "incidents"

_VALUES_V5 = (
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
    "humidity_fault",
)

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


def _check_expr(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"fault_type IS NULL OR fault_type IN ({quoted})"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            _check_expr(_VALUES_V5),
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE_NAME) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            _check_expr(_VALUES_V4),
        )

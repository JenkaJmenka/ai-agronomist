"""Deduplicate readings and enforce unique tenant/greenhouse/timestamp.

Revision ID: 013
Revises: 012
Create Date: 2026-06-25

"""
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def _dedupe_table(table: str) -> None:
    op.execute(
        f"""
        DELETE FROM {table}
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM {table}
            GROUP BY tenant_id, greenhouse_id, timestamp
        )
        """
    )


def upgrade() -> None:
    _dedupe_table("sensor_readings")
    _dedupe_table("raw_readings")
    op.create_unique_constraint(
        "uq_sensor_readings_tenant_greenhouse_timestamp",
        "sensor_readings",
        ["tenant_id", "greenhouse_id", "timestamp"],
    )
    op.create_unique_constraint(
        "uq_raw_readings_tenant_greenhouse_timestamp",
        "raw_readings",
        ["tenant_id", "greenhouse_id", "timestamp"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_raw_readings_tenant_greenhouse_timestamp",
        "raw_readings",
        type_="unique",
    )
    op.drop_constraint(
        "uq_sensor_readings_tenant_greenhouse_timestamp",
        "sensor_readings",
        type_="unique",
    )

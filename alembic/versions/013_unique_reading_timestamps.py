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
    
    with op.batch_alter_table("sensor_readings", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_sensor_readings_tenant_greenhouse_timestamp",
            ["tenant_id", "greenhouse_id", "timestamp"],
        )
        
    with op.batch_alter_table("raw_readings", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_raw_readings_tenant_greenhouse_timestamp",
            ["tenant_id", "greenhouse_id", "timestamp"],
        )


def downgrade() -> None:
    with op.batch_alter_table("raw_readings", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_raw_readings_tenant_greenhouse_timestamp",
            type_="unique",
        )
        
    with op.batch_alter_table("sensor_readings", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_sensor_readings_tenant_greenhouse_timestamp",
            type_="unique",
        )

"""Collision-safe DB writes for injected synthetic rows.

Split out of ``synthetic_anomalies.py`` for the 300-line guardrail: this
module owns the one function that actually writes injected rows to the
database, plus the small helpers it needs. Everything here performs I/O
(unlike ``synthetic_anomaly_generators.py``, which builds values and ORM
objects in memory only) -- see that module's docstring for the split
rationale and ``synthetic_anomalies.py``'s docstring for the provenance
contract these writes must honor.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import RawReading, SensorReading
from src.db.upsert import READING_CONFLICT_COLS, dialect_insert


def normalize_ts(ts: datetime) -> datetime:
    """UTC-naive normalization so a timestamp compares equal before and after
    a DB round-trip. SQLite's ``DateTime(timezone=True)`` does not guarantee
    an aware datetime back on SELECT/RETURNING the way Postgres does, so
    raw object-identity/tzinfo equality between an injected value and its
    RETURNING echo cannot be relied on."""
    if ts.tzinfo is not None:
        return ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def _sensor_row_values(row: SensorReading) -> dict:
    """Column values for one injected SensorReading (excludes the DB-generated
    ``id``/``created_at``, which a VALUES-list insert must not set)."""
    return {
        "tenant_id": row.tenant_id,
        "greenhouse_id": row.greenhouse_id,
        "timestamp": row.timestamp,
        "temperature": row.temperature,
        "humidity": row.humidity,
        "co2": row.co2,
        "light": row.light,
        "anomaly_score": row.anomaly_score,
        "anomaly_score_raw": row.anomaly_score_raw,
        "score_scale": row.score_scale,
        "anomaly_type": row.anomaly_type,
        "source": row.source,
    }


def _raw_row_values(row: RawReading) -> dict:
    """Column values for one injected RawReading (excludes the DB-generated
    ``id``/``created_at``)."""
    return {
        "tenant_id": row.tenant_id,
        "greenhouse_id": row.greenhouse_id,
        "timestamp": row.timestamp,
        "source": row.source,
        "data": row.data,
    }


async def persist_injection_results(
    db: AsyncSession, sensor_records: list[SensorReading], raw_records: list[RawReading]
) -> tuple[set[datetime], int]:
    """Write the injected rows via ON CONFLICT DO NOTHING; invalidate caches.

    Never DO UPDATE -- see ``synthetic_anomalies.py``'s module docstring /
    ADR-010: a synthetic row must not overwrite a real reading's data or
    provenance.

    Raw rows are inserted only for timestamps whose sensor row was actually
    inserted (not merely attempted), using the sensor insert's own RETURNING
    to know which those are. This keeps the two tables paired on a collision:
    with independent per-table DO NOTHING, a timestamp could land in one
    table and be skipped in the other, leaving an orphaned half of the pair.
    ``raw_records`` may legitimately be empty (``has_raw_cols=False``) --
    this is not assumed to be 1:1 with ``sensor_records``.

    Returns the set of timestamps actually written to ``sensor_readings``
    (normalized -- see ``normalize_ts``) and the count of raw rows actually
    written, so the caller can report real, not requested, counts. Caller is
    responsible for cache invalidation after commit.
    """
    insert_fn = dialect_insert(db)
    conflict_cols = list(READING_CONFLICT_COLS)

    inserted_sensor_timestamps: set[datetime] = set()
    sensor_rows = [_sensor_row_values(r) for r in sensor_records]
    if sensor_rows:
        stmt = (
            insert_fn(SensorReading)
            .values(sensor_rows)
            .on_conflict_do_nothing(index_elements=conflict_cols)
            .returning(SensorReading.timestamp)
        )
        result = await db.execute(stmt)
        inserted_sensor_timestamps = {normalize_ts(ts) for ts in result.scalars().all()}

    raw_rows = [
        _raw_row_values(r)
        for r in raw_records
        if normalize_ts(r.timestamp) in inserted_sensor_timestamps
    ]
    raw_inserted = 0
    if raw_rows:
        raw_stmt = (
            insert_fn(RawReading)
            .values(raw_rows)
            .on_conflict_do_nothing(index_elements=conflict_cols)
            .returning(RawReading.timestamp)
        )
        raw_result = await db.execute(raw_stmt)
        raw_inserted = len(raw_result.scalars().all())

    await db.commit()
    return inserted_sensor_timestamps, raw_inserted

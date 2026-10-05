"""Shared helpers for the synthetic anomaly inject/remove service tests.

``test_synthetic_anomalies_service.py`` (injection side) and
``test_synthetic_anomalies_removal.py`` (removal side) were one 450-line module
before the 300-line guardrail split them; both need the same tenant constants,
history seeder and row builders, so this module owns them rather than one test
file importing the other's internals.

The builders bake in ``TENANT``/``GREENHOUSE``, which makes them too narrow for
``tests/api/conftest.py`` — that would broadcast service-specific helpers to
every api test.  The generic ``db_session`` fixture does live in conftest,
because a fixture is what pytest discovers on its own.
"""

from __future__ import annotations

import datetime
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.synthetic_anomalies import inject_synthetic_anomalies
from src.db.models import RawReading, SensorReading

TENANT = "tenant_synthetic_service"
OTHER_TENANT = "tenant_synthetic_other"
GREENHOUSE = "gh_synthetic_service"

RNG_SEED = 90210
HISTORY_ROWS = 60
HISTORY_START = datetime.datetime(2026, 4, 1, 0, 0, tzinfo=datetime.timezone.utc)
HISTORY_STEP = datetime.timedelta(minutes=5)
WINDOW_START = "2026-04-05T00:00:00+00:00"
WINDOW_END = "2026-04-07T00:00:00+00:00"

DEFAULT_WINDOW_HOURS = 48


async def seed_raw_history(db: AsyncSession, *, extra_columns: dict | None = None) -> None:
    """Seed RawReading history with per-column Gaussian noise (never constants)."""
    rng = random.Random(RNG_SEED)
    db.add_all(
        [
            RawReading(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                timestamp=HISTORY_START + i * HISTORY_STEP,
                source="csv",
                data={
                    "temperature": round(rng.gauss(23.0, 1.5), 2),
                    "humidity": round(rng.gauss(63.0, 4.0), 2),
                    "co2": round(rng.gauss(420.0, 30.0), 2),
                    "light": round(rng.gauss(400.0, 90.0), 2),
                    **(extra_columns or {}),
                },
            )
            for i in range(HISTORY_ROWS)
        ]
    )
    await db.commit()


async def inject(db: AsyncSession, **overrides) -> dict:
    params = {
        "tenant_id": TENANT,
        "greenhouse_id": GREENHOUSE,
        "n_normal": 10,
        "n_anomalies": 10,
        "anomaly_type": "co2_surge",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
    }
    params.update(overrides)
    return await inject_synthetic_anomalies(db, **params)


def source_marker_for(anomaly_type: str | None) -> str | None:
    """Mirror the ``source`` marker ``inject_synthetic_anomalies`` writes.

    Keeps the hand-built removal fixtures honest about the provenance tag the
    service really emits (``synthetic_anomalies.py:298``) — removal keys off
    it, so a fixture that omitted it would describe a row the system cannot
    produce.

    This is a *mirror*, not a source of truth: it is deliberately unused by
    ``test_should_remove_every_row_a_real_injection_just_wrote``, which closes
    the writer↔reader loop with production code on both sides so a change to
    the marker format cannot pass by agreeing with this function.
    """
    if anomaly_type is None:
        return None
    if anomaly_type == "synthetic_context":
        return "synthetic:context"
    return f"synthetic:{anomaly_type}"


def synthetic_sensor_row(
    *, anomaly_type: str | None, index: int, tenant_id: str = TENANT
) -> SensorReading:
    return SensorReading(
        tenant_id=tenant_id,
        greenhouse_id=GREENHOUSE,
        timestamp=HISTORY_START + index * HISTORY_STEP,
        temperature=22.0,
        humidity=60.0,
        co2=430.0,
        light=300.0,
        anomaly_score=0.9,
        anomaly_type=anomaly_type,
        source=source_marker_for(anomaly_type),
    )


def detector_labelled_sensor_row(*, anomaly_type: str, index: int) -> SensorReading:
    """A real measured row that carries a detector label but no synthetic marker."""
    return SensorReading(
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        timestamp=HISTORY_START + index * HISTORY_STEP,
        temperature=22.0,
        humidity=60.0,
        co2=430.0,
        light=300.0,
        anomaly_score=0.9,
        anomaly_type=anomaly_type,
        source="live",
    )


def synthetic_raw_row(*, source: str, index: int, tenant_id: str = TENANT) -> RawReading:
    return RawReading(
        tenant_id=tenant_id,
        greenhouse_id=GREENHOUSE,
        timestamp=HISTORY_START + index * HISTORY_STEP,
        source=source,
        data={"co2": 430.0},
    )


async def sensor_rows_of(db: AsyncSession, tenant_id: str = TENANT) -> list[SensorReading]:
    """Return every SensorReading row of the tenant, read back from the DB."""
    rows = await db.execute(select(SensorReading).where(SensorReading.tenant_id == tenant_id))
    return list(rows.scalars().all())


async def count_sensor_rows(db: AsyncSession, tenant_id: str = TENANT) -> int:
    return len(await sensor_rows_of(db, tenant_id))


async def raw_sources(db: AsyncSession, tenant_id: str = TENANT) -> list[str | None]:
    rows = await db.execute(select(RawReading).where(RawReading.tenant_id == tenant_id))
    return [row.source for row in rows.scalars().all()]

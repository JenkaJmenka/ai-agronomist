"""Shared row builders and survivor queries for the synthetic-marker tests.

``test_synthetic_marker_contract.py`` and
``test_synthetic_marker_contract_live_wipe.py`` are one contract split across
two files for the 300-line guardrail, so they need the same tenant constants
and row builders.  They used to come from a cross-module import of the first
file's underscore-private helpers; this module owns them instead, so the
dependency is explicit and a rename fails loudly at import time.

The builders bake in ``TENANT``/``GREENHOUSE``, which makes them too narrow for
``tests/api/conftest.py`` — that would broadcast marker-specific helpers to
every api test.  The generic ``db_session`` fixture does live in conftest,
because a fixture is what pytest discovers on its own.

The second half of the module serves the other marker pair,
``test_upload_overwrites_synthetic_rows.py`` and
``test_upload_overwrites_non_synthetic_rows.py`` — the same 300-line split, on
the upload-collision side of the contract.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import SYNTHETIC_SOURCE_PREFIX
from src.db.models import RawReading, SensorReading
from src.db.upsert import bulk_upsert_rows

TENANT = "tenant_synthetic_marker"
GREENHOUSE = "gh_synthetic_marker"

# Label a real detector would persist once detection writes anomaly_type.
# UPPER_CASE by convention — see src/api/services/fault_classification.py:73-76
# (setpoint_scada_detector, band_breach_detector).
REAL_DETECTOR_LABEL = "UNDER_HEATING"
SYNTHETIC_TYPE = "co2_surge"
SYNTHETIC_MARKER = f"{SYNTHETIC_SOURCE_PREFIX}{SYNTHETIC_TYPE}"
LIVE_SOURCE = "live"

T0 = datetime.datetime(2026, 5, 1, 0, 0, tzinfo=datetime.timezone.utc)
STEP = datetime.timedelta(minutes=5)


def sensor_row(
    *,
    index: int,
    anomaly_type: str | None,
    source: str | None,
    anomaly_score: float,
    score_scale: str | None = None,
) -> SensorReading:
    return SensorReading(
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        timestamp=T0 + index * STEP,
        temperature=21.5,
        humidity=61.0,
        co2=440.0,
        light=320.0,
        anomaly_score=anomaly_score,
        score_scale=score_scale,
        anomaly_type=anomaly_type,
        source=source,
    )


def raw_row(*, index: int, source: str) -> RawReading:
    return RawReading(
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        timestamp=T0 + index * STEP,
        source=source,
        data={"temperature": 21.5, "humidity": 61.0, "co2": 440.0, "light": 320.0},
    )


async def surviving_sensor_rows(db: AsyncSession) -> set[tuple[str | None, str | None]]:
    """Return ``(source, anomaly_type)`` for every surviving row of the tenant."""
    rows = await db.execute(select(SensorReading).where(SensorReading.tenant_id == TENANT))
    return {(row.source, row.anomaly_type) for row in rows.scalars().all()}


async def surviving_raw_sources(db: AsyncSession) -> list[str | None]:
    rows = await db.execute(select(RawReading).where(RawReading.tenant_id == TENANT))
    return sorted(row.source for row in rows.scalars().all())


# ---------------------------------------------------------------------------
# Upload-collision side of the contract
#
# Its own tenant: the two upload files drive the production upsert helper, not
# the removal/wipe services, so a stray row from one group must never be able
# to satisfy an assertion in the other.
# ---------------------------------------------------------------------------

UPLOAD_TENANT = "tenant_upload_vs_synthetic"
UPLOAD_GREENHOUSE = "gh_upload_vs_synthetic"

UPLOAD_T = datetime.datetime(2026, 6, 1, 12, 0, tzinfo=datetime.timezone.utc)

# Measured values the upload carries — deliberately unlike the synthetic row's,
# so "the real values landed" cannot pass by coincidence.
REAL_VALUES = {"temperature": 24.25, "humidity": 58.5, "co2": 455.0, "light": 615.0}
SYNTHETIC_VALUES = {"temperature": 21.0, "humidity": 70.0, "co2": 1480.0, "light": 120.0}


def upload_collision_row(**overrides) -> SensorReading:
    """A row in the exact state ``inject_synthetic_anomalies`` leaves behind."""
    fields = {
        "tenant_id": UPLOAD_TENANT,
        "greenhouse_id": UPLOAD_GREENHOUSE,
        "timestamp": UPLOAD_T,
        **SYNTHETIC_VALUES,
        "anomaly_score": 0.93,
        "anomaly_score_raw": None,
        "score_scale": "synthetic",
        "anomaly_type": SYNTHETIC_TYPE,
        "source": SYNTHETIC_MARKER,
    }
    fields.update(overrides)
    return SensorReading(**fields)


async def upload_upsert(db: AsyncSession, timestamp: datetime.datetime) -> None:
    """Upsert one real measured row, exactly as ``upload_csv`` does.

    Mirrors ``src/api/routes/upload.py:112-127``: the four canonical sensor
    floats in ``update_cols``, ``anomaly_score`` preserved when the incoming
    value is NULL.  No ``source``/``anomaly_type`` is sent, because a CSV
    upload carries neither.
    """
    await bulk_upsert_rows(
        db,
        SensorReading,
        [
            {
                "tenant_id": UPLOAD_TENANT,
                "greenhouse_id": UPLOAD_GREENHOUSE,
                "timestamp": timestamp,
                **REAL_VALUES,
            }
        ],
        update_cols=["temperature", "humidity", "co2", "light"],
        preserve_when_null=["anomaly_score"],
    )
    await db.commit()


async def only_upload_row(db: AsyncSession) -> SensorReading:
    rows = (
        await db.execute(
            select(SensorReading).where(SensorReading.tenant_id == UPLOAD_TENANT)
        )
    ).scalars().all()
    assert len(rows) == 1, f"expected exactly one row for the tenant, found {len(rows)}"
    return rows[0]


def measured_values_of(row: SensorReading) -> dict[str, float]:
    return {
        "temperature": row.temperature,
        "humidity": row.humidity,
        "co2": row.co2,
        "light": row.light,
    }

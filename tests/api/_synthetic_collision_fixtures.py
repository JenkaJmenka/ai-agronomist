"""Shared helpers for the synthetic-injection collision tests.

PR #90 anchored the injection window to the newest stored timestamp, so
injected rows now land on top of dense real replay data instead of in empty
wall-clock space.  Reproducing that collision deterministically needs a real
5-minute grid seeded across the exact window the injection is asked for —
which ``tests/api/_synthetic_service_fixtures.py`` deliberately does not
provide (its history sits *outside* its injection window, so nothing ever
collides there).  This module owns the dense-grid seeder and the arithmetic
constants; the assertions live in
``tests/api/test_synthetic_injection_collision.py`` (the core skip/pairing/
honest-count contract) and
``tests/api/test_synthetic_injection_collision_boundaries.py`` (the zero/all
boundaries), split for the 300-line guardrail.

The tenant/greenhouse constants are reused from the sibling fixtures module so
the two suites cannot drift apart on scope identity.
"""

from __future__ import annotations

import datetime
import random

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.synthetic_anomalies import inject_synthetic_anomalies
from src.config import SYNTHETIC_SOURCE_PREFIX
from src.db.models import RawReading, SensorReading
from tests.api._synthetic_service_fixtures import GREENHOUSE, TENANT

# Newest stored timestamp the panel anchors its window to, and the 24h window
# ending there.  Fixed (not wall-clock) so the grid, the candidate timestamps
# and the collision set are all reproducible.
MAX_TS = datetime.datetime(2026, 4, 7, 0, 0, tzinfo=datetime.timezone.utc)
WINDOW_HOURS = 24
WINDOW_START_DT = MAX_TS - datetime.timedelta(hours=WINDOW_HOURS)
WINDOW_START = WINDOW_START_DT.isoformat()
WINDOW_END = MAX_TS.isoformat()

# The real replay grid: 5-minute cadence, which is what the CSV replay data
# this feature is used against actually has.
GRID_STEP = datetime.timedelta(seconds=300)

# Panel defaults from the inject-test UI.
PANEL_NORMAL = 96
PANEL_ANOMALIES = 8
PANEL_TOTAL = PANEL_NORMAL + PANEL_ANOMALIES

# int(86400 / 104) — the service floors the interval, so injected row i lands
# at window_start + i * 830s.  830i is a multiple of the 300s grid step exactly
# when i is a multiple of 30 (gcd(83, 30) == 1), so 4 of the 104 rows collide.
PANEL_INTERVAL_SECONDS = 830
COLLIDING_INDEXES = frozenset({0, 30, 60, 90})

# Seeds: GRID_SEED drives the real-row noise through a private Random instance
# (so it cannot disturb the global RNG the service samples from); COLLISION_SEED
# is the global seed under which random.sample picks anomaly position 0 — one of
# the colliding indexes — which is what makes the anomaly-collides case real.
GRID_SEED = 5150
COLLISION_SEED = 1234

REAL_SOURCE = "csv"

# anomaly_type the service writes on its background filler rows.
CONTEXT_LABEL = "synthetic_context"


def naive_utc(ts: datetime.datetime) -> datetime.datetime:
    """UTC-naive form of *ts*, for comparing a timestamp across a DB round trip.

    Deliberately a test-side reimplementation rather than an import of
    ``synthetic_anomaly_persistence.normalize_ts``: the persistence module uses
    its own normalization to decide which raw rows to write, so a test that
    reused it could agree with a broken one.
    """
    if ts.tzinfo is not None:
        return ts.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return ts


def candidate_timestamps(
    total: int = PANEL_TOTAL, interval_seconds: int = PANEL_INTERVAL_SECONDS
) -> list[datetime.datetime]:
    """Every timestamp an injection run of *total* rows will attempt to write."""
    return [
        WINDOW_START_DT + datetime.timedelta(seconds=i * interval_seconds)
        for i in range(total)
    ]


def grid_timestamps(
    start: datetime.datetime = WINDOW_START_DT, end: datetime.datetime = MAX_TS
) -> list[datetime.datetime]:
    """Real-grid timestamps from *start* through *end* inclusive, every 300s."""
    steps = int((end - start).total_seconds() // GRID_STEP.total_seconds())
    return [start + i * GRID_STEP for i in range(steps + 1)]


def real_sensor_values(index: int) -> dict:
    """Canonical-looking values for one real reading, distinct per *index*.

    Per-index values matter: a collision test that seeded identical rows could
    not tell "the real row survived untouched" from "a synthetic row happened
    to write the same numbers".
    """
    return {
        "temperature": round(22.0 + index * 0.001, 4),
        "humidity": round(61.0 + index * 0.002, 4),
        "co2": round(415.0 + index * 0.01, 4),
        "light": round(305.0 + index * 0.02, 4),
    }


def _real_raw_data(rng: random.Random) -> dict:
    """One real RawReading payload with per-column Gaussian noise (never
    constants, so the service learns a non-degenerate range per column)."""
    return {
        "temperature": round(rng.gauss(23.0, 1.5), 2),
        "humidity": round(rng.gauss(63.0, 4.0), 2),
        "co2": round(rng.gauss(420.0, 30.0), 2),
        "light": round(rng.gauss(400.0, 90.0), 2),
    }


async def seed_real_raw_grid(
    db: AsyncSession, timestamps: list[datetime.datetime] | None = None
) -> None:
    """Seed RawReading rows only — enough for column learning, no sensor rows
    (so nothing can collide on the sensor side)."""
    rng = random.Random(GRID_SEED)
    db.add_all(
        [
            RawReading(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                timestamp=ts,
                source=REAL_SOURCE,
                data=_real_raw_data(rng),
            )
            for ts in (timestamps if timestamps is not None else grid_timestamps())
        ]
    )
    await db.commit()


async def seed_real_sensor_grid(
    db: AsyncSession, timestamps: list[datetime.datetime] | None = None
) -> list[datetime.datetime]:
    """Seed SensorReading rows only — collisions, but no raw column history
    (so the service resolves ``has_raw_cols=False`` and builds no raw rows)."""
    resolved = timestamps if timestamps is not None else grid_timestamps()
    db.add_all(
        [
            SensorReading(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                timestamp=ts,
                anomaly_score=0.1,
                score_scale="canonical",
                anomaly_type=None,
                source=REAL_SOURCE,
                **real_sensor_values(i),
            )
            for i, ts in enumerate(resolved)
        ]
    )
    await db.commit()
    return resolved


async def seed_real_grid(
    db: AsyncSession, timestamps: list[datetime.datetime] | None = None
) -> list[datetime.datetime]:
    """Seed paired real SensorReading + RawReading rows on the 5-minute grid.

    Returns the seeded timestamps.  Real rows carry ``source='csv'`` and no
    ``anomaly_type``, which is what makes a provenance overwrite detectable.
    """
    resolved = await seed_real_sensor_grid(db, timestamps)
    await seed_real_raw_grid(db, resolved)
    return resolved


async def inject_panel(db: AsyncSession, **overrides) -> dict:
    """Run one injection with the inject-test panel's own defaults."""
    params = {
        "tenant_id": TENANT,
        "greenhouse_id": GREENHOUSE,
        "n_normal": PANEL_NORMAL,
        "n_anomalies": PANEL_ANOMALIES,
        "anomaly_type": "co2_surge",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
    }
    params.update(overrides)
    return await inject_synthetic_anomalies(db, **params)


async def _synthetic_rows(db: AsyncSession, model: type) -> list:
    rows = await db.execute(
        select(model).where(
            model.tenant_id == TENANT,
            model.greenhouse_id == GREENHOUSE,
            model.source.like(f"{SYNTHETIC_SOURCE_PREFIX}%"),
        )
    )
    return list(rows.scalars().all())


async def synthetic_sensor_timestamps(db: AsyncSession) -> set[datetime.datetime]:
    """UTC-naive timestamps of every synthetic-marked SensorReading in scope."""
    return {naive_utc(row.timestamp) for row in await _synthetic_rows(db, SensorReading)}


async def synthetic_raw_timestamps(db: AsyncSession) -> set[datetime.datetime]:
    """UTC-naive timestamps of every synthetic-marked RawReading in scope."""
    return {naive_utc(row.timestamp) for row in await _synthetic_rows(db, RawReading)}


async def count_synthetic(db: AsyncSession, model: type) -> int:
    """``SELECT COUNT(*)`` of synthetic-marked rows in scope.

    Computed straight from the DB, independently of the response payload the
    tests compare it against — that independence is the whole point of case 3.
    """
    return await db.scalar(
        select(func.count())
        .select_from(model)
        .where(
            model.tenant_id == TENANT,
            model.greenhouse_id == GREENHOUSE,
            model.source.like(f"{SYNTHETIC_SOURCE_PREFIX}%"),
        )
    )


async def count_synthetic_anomaly_rows(db: AsyncSession) -> int:
    """Synthetic sensor rows carrying a real anomaly label, not context filler."""
    return await db.scalar(
        select(func.count())
        .select_from(SensorReading)
        .where(
            SensorReading.tenant_id == TENANT,
            SensorReading.greenhouse_id == GREENHOUSE,
            SensorReading.source.like(f"{SYNTHETIC_SOURCE_PREFIX}%"),
            SensorReading.anomaly_type != CONTEXT_LABEL,
        )
    )


async def real_row_at(db: AsyncSession, ts: datetime.datetime) -> SensorReading | None:
    """Read one SensorReading back from the DB, bypassing the identity map.

    ``db_session`` is built with ``expire_on_commit=False``, so seeded instances
    stay live in the session; without ``expire_all`` a collision test could
    assert against its own in-memory copy and never see a DB overwrite.
    """
    db.expire_all()
    return await db.scalar(
        select(SensorReading).where(
            SensorReading.tenant_id == TENANT,
            SensorReading.greenhouse_id == GREENHOUSE,
            SensorReading.timestamp == ts,
        )
    )

"""Regression tests: synthetic injection must never conflate climate ``co2``
with gas-panel ``gas_co2``.

``co2`` is a climate channel (~380-520 ppm ambient); ``gas_co2`` is a dedicated
gas-panel instrument (0-5000 ppm) — a different physical sensor, per the
invariant stated in ``src/vendors/csv_adapter.py:346``.  Every entry in
``_TYPE_PROFILES`` is a *climate* anomaly type, and the Gas Monitoring page
filters exact on ``src.config.GAS_SENSOR_COLUMNS``.

Two bugs are pinned here:

1. Targeted types spiked gas columns by substring: ``co2_surge``
   (``raw_keywords = ["co2"]``) matched ``gas_co2`` and fabricated gas-panel
   spikes for a purely climate anomaly.
2. ``_pick_col`` returned the first *substring* match in dict insertion order —
   i.e. CSV column order — so a file listing ``gas_co2`` before ``co2`` wrote
   the gas-panel value into canonical ``SensorReading.co2``.

Seeding note: every column is seeded from a real distribution
(``random.gauss``), never constants.  With zero variance ``_normal_value``
degenerates and ``_anomaly_value_directed`` floors ``span`` at 1.0, making
spikes ~1 unit wide and effectively invisible — that artifact produces false
negatives in exactly these assertions.
"""

from __future__ import annotations

import datetime
import random

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services import synthetic_anomalies as synthetic_anomalies_service
from src.db.models import Base, RawReading, SensorReading

TENANT = "tenant_synthetic_gas"
GREENHOUSE = "gh_synthetic_gas"

# Deterministic seeds: one for the fixture data, one for the service's module
# level ``random`` usage.  Assertions hold for any seed; the seeds only make
# failures reproducible.
_HISTORY_SEED = 20260919
_SERVICE_SEED = 4242

_HISTORY_ROWS = 200
_HISTORY_START = datetime.datetime(2026, 3, 1, 0, 0, tzinfo=datetime.timezone.utc)
_HISTORY_STEP = datetime.timedelta(minutes=5)

# Injection window sits after the seeded history so the unique constraint on
# (tenant, greenhouse, timestamp) can never collide with fixture rows.
_WINDOW_START = "2026-03-05T00:00:00+00:00"
_WINDOW_END = "2026-03-07T00:00:00+00:00"

# Climate co2 and gas_co2 are seeded into disjoint value bands so a value that
# landed in the wrong canonical field is unambiguous.  Even the widest climate
# co2 spike (max + 1.5 * span) stays far below the gas band.
_COLUMN_DISTRIBUTIONS: dict[str, tuple[float, float]] = {
    "temperature": (23.0, 1.5),
    "humidity": (63.0, 4.0),
    "co2": (420.0, 30.0),
    "gas_co2": (2000.0, 120.0),
    "light": (400.0, 90.0),
}

_GAS_FIRST_ORDER = ["temperature", "humidity", "gas_co2", "co2", "light"]
_CLIMATE_FIRST_ORDER = ["temperature", "humidity", "co2", "gas_co2", "light"]

# Rounding tolerance: canonical fields are re-rounded (co2 to 1 decimal) after
# _normal_value has already rounded to 2.
_ROUNDING_TOLERANCE = 0.1


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def seed_service_rng() -> None:
    """Pin the module-level RNG the injection service draws from."""
    random.seed(_SERVICE_SEED)


async def _seed_raw_history(
    db: AsyncSession, column_order: list[str]
) -> dict[str, tuple[float, float]]:
    """Seed RawReading history with per-column Gaussian noise.

    ``column_order`` controls the JSON key order, which is what the service
    later sees as "CSV column order".  Returns the observed (min, max) per
    column — the range the injector learns from.
    """
    rng = random.Random(_HISTORY_SEED)
    series = {
        col: [round(rng.gauss(mean, sigma), 2) for _ in range(_HISTORY_ROWS)]
        for col, (mean, sigma) in _COLUMN_DISTRIBUTIONS.items()
    }
    db.add_all(
        [
            RawReading(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                timestamp=_HISTORY_START + i * _HISTORY_STEP,
                source="csv",
                data={col: series[col][i] for col in column_order},
            )
            for i in range(_HISTORY_ROWS)
        ]
    )
    await db.commit()
    return {col: (min(values), max(values)) for col, values in series.items()}


async def _inject(db: AsyncSession, anomaly_type: str, *, n_anomalies: int = 20) -> dict:
    return await synthetic_anomalies_service.inject_synthetic_anomalies(
        db,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        n_normal=20,
        n_anomalies=n_anomalies,
        anomaly_type=anomaly_type,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )


async def _raw_rows_with_source(db: AsyncSession, source: str) -> list[RawReading]:
    rows = await db.execute(
        select(RawReading)
        .where(RawReading.tenant_id == TENANT, RawReading.source == source)
        .order_by(RawReading.timestamp)
    )
    return list(rows.scalars().all())


async def _all_sensor_rows(db: AsyncSession) -> list[SensorReading]:
    rows = await db.execute(
        select(SensorReading)
        .where(SensorReading.tenant_id == TENANT)
        .order_by(SensorReading.timestamp)
    )
    return list(rows.scalars().all())


def _span(column_range: tuple[float, float]) -> float:
    col_min, col_max = column_range
    return max(col_max - col_min, 1.0)


# ---------------------------------------------------------------------------
# Bug 1 — targeted types must not spike gas-panel columns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_spike_climate_co2_raw_column_for_co2_surge(
    db_session: AsyncSession,
) -> None:
    """The targeted climate column is driven far outside its seeded range."""
    ranges = await _seed_raw_history(db_session, _GAS_FIRST_ORDER)

    await _inject(db_session, "co2_surge")

    spike_rows = await _raw_rows_with_source(db_session, "synthetic:co2_surge")
    assert len(spike_rows) == 20

    co2_min, co2_max = ranges["co2"]
    # direction "high" => col_max + span * uniform(0.5, 1.5)
    threshold = co2_max + 0.4 * _span(ranges["co2"])
    assert all(row.data["co2"] > threshold for row in spike_rows), (
        f"climate co2 spikes should clear {threshold:.2f} "
        f"(seeded range {co2_min:.2f}-{co2_max:.2f})"
    )


@pytest.mark.asyncio
async def test_should_leave_gas_co2_raw_column_inside_seeded_range_for_co2_surge(
    db_session: AsyncSession,
) -> None:
    """``co2_surge`` is a climate anomaly — the gas panel must stay quiet."""
    ranges = await _seed_raw_history(db_session, _GAS_FIRST_ORDER)

    await _inject(db_session, "co2_surge")

    spike_rows = await _raw_rows_with_source(db_session, "synthetic:co2_surge")
    gas_min, gas_max = ranges["gas_co2"]
    out_of_band = [
        row.data["gas_co2"]
        for row in spike_rows
        if not (gas_min - _ROUNDING_TOLERANCE <= row.data["gas_co2"] <= gas_max + _ROUNDING_TOLERANCE)
    ]
    assert out_of_band == [], (
        f"gas_co2 must stay inside its seeded range {gas_min:.2f}-{gas_max:.2f}; "
        f"fabricated gas-panel spikes: {out_of_band[:5]}"
    )


@pytest.mark.asyncio
async def test_should_keep_gas_co2_quiet_for_every_targeted_anomaly_type(
    db_session: AsyncSession,
) -> None:
    """No climate-targeted profile may ever move a gas-panel column."""
    ranges = await _seed_raw_history(db_session, _GAS_FIRST_ORDER)
    gas_min, gas_max = ranges["gas_co2"]

    targeted_types = [
        name
        for name, profile in synthetic_anomalies_service._TYPE_PROFILES.items()
        if profile[3]
    ]
    assert targeted_types, "expected at least one targeted profile"

    disturbed: dict[str, float] = {}
    for anomaly_type in targeted_types:
        await _inject(db_session, anomaly_type, n_anomalies=10)
        for row in await _raw_rows_with_source(db_session, f"synthetic:{anomaly_type}"):
            gas_value = row.data["gas_co2"]
            if not (gas_min - _ROUNDING_TOLERANCE <= gas_value <= gas_max + _ROUNDING_TOLERANCE):
                disturbed[anomaly_type] = gas_value
        # Clear spike *and* context rows: the next iteration reuses the same
        # window timestamps, which the raw-readings unique constraint rejects.
        await synthetic_anomalies_service.delete_synthetic_anomalies(
            db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
        )

    assert disturbed == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("anomaly_type", ["random", "multi_sensor"])
async def test_should_spike_every_raw_column_including_gas_for_untargeted_types(
    db_session: AsyncSession, anomaly_type: str
) -> None:
    """Untargeted profiles (``raw_keywords == []``) deliberately spike everything.

    This asymmetry against the targeted types is intentional — pinned so it is
    not "fixed" away alongside the gas-isolation rule.
    """
    ranges = await _seed_raw_history(db_session, _GAS_FIRST_ORDER)

    await _inject(db_session, anomaly_type)

    spike_rows = await _raw_rows_with_source(db_session, f"synthetic:{anomaly_type}")
    assert len(spike_rows) == 20

    untouched: dict[str, float] = {}
    for row in spike_rows:
        for col, (col_min, col_max) in ranges.items():
            if col_min <= row.data[col] <= col_max:
                untouched[col] = row.data[col]
    assert untouched == {}, f"{anomaly_type} must spike every raw column, incl. gas: {untouched}"


# ---------------------------------------------------------------------------
# Bug 2 — canonical co2 must resolve to the climate column, not the gas panel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column_order", [_GAS_FIRST_ORDER, _CLIMATE_FIRST_ORDER], ids=["gas_first", "climate_first"]
)
async def test_should_store_climate_co2_in_canonical_column_regardless_of_csv_order(
    db_session: AsyncSession, column_order: list[str]
) -> None:
    """A temperature anomaly leaves co2 normal — canonical co2 must be climate.

    If the gas-panel value leaks into ``SensorReading.co2`` the stored value
    lands in the ~2000 ppm gas band instead of the ~420 ppm climate band.
    """
    ranges = await _seed_raw_history(db_session, column_order)

    await _inject(db_session, "temperature_spike")

    co2_min, co2_max = ranges["co2"]
    leaked = [
        row.co2
        for row in await _all_sensor_rows(db_session)
        if not (co2_min - _ROUNDING_TOLERANCE <= row.co2 <= co2_max + _ROUNDING_TOLERANCE)
    ]
    assert leaked == [], (
        f"canonical co2 must stay in the climate band {co2_min:.1f}-{co2_max:.1f}; "
        f"leaked values: {leaked[:5]}"
    )


@pytest.mark.asyncio
async def test_should_keep_canonical_co2_below_the_gas_band_during_co2_surge(
    db_session: AsyncSession,
) -> None:
    """Even a spiked climate co2 stays orders below the gas-panel band."""
    ranges = await _seed_raw_history(db_session, _GAS_FIRST_ORDER)

    await _inject(db_session, "co2_surge")

    gas_min, _gas_max = ranges["gas_co2"]
    sensor_rows = await _all_sensor_rows(db_session)
    assert sensor_rows, "injection should have written canonical rows"
    assert all(row.co2 < gas_min for row in sensor_rows)


# ---------------------------------------------------------------------------
# Bug 2 — _pick_col resolution rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "col_values",
    [
        {"gas_co2": 1200.0, "co2": 430.0},
        {"co2": 430.0, "gas_co2": 1200.0},
    ],
    ids=["gas_first", "climate_first"],
)
def test_should_resolve_climate_co2_under_both_column_orderings(
    col_values: dict[str, float]
) -> None:
    resolved = synthetic_anomalies_service._pick_col(col_values, ["co2"], fallback=999.0)
    assert resolved == 430.0


def test_should_return_fallback_when_only_gas_columns_are_present() -> None:
    """A gas-panel instrument can never stand in for its climate namesake."""
    resolved = synthetic_anomalies_service._pick_col(
        {"gas_co2": 1200.0, "co": 7.5}, ["co2"], fallback=415.0
    )
    assert resolved == 415.0


def test_should_prefer_higher_priority_keyword_over_column_order() -> None:
    """Caller-supplied keyword priority beats CSV insertion order."""
    resolved = synthetic_anomalies_service._pick_col(
        {"temperature": 21.0, "greenhouse_temp": 24.5},
        ["greenhouse_temp", "temperature", "temp"],
        fallback=23.0,
    )
    assert resolved == 24.5


def test_should_substring_match_measured_co2_when_no_exact_co2_column_exists() -> None:
    """Exact-match preference must not break vendor-prefixed climate columns."""
    resolved = synthetic_anomalies_service._pick_col(
        {"gas_co2": 1200.0, "measured_co2": 455.0}, ["co2"], fallback=999.0
    )
    assert resolved == 455.0


def test_should_return_fallback_when_no_column_matches_any_keyword() -> None:
    resolved = synthetic_anomalies_service._pick_col(
        {"pressure": 101.3}, ["light"], fallback=200.0
    )
    assert resolved == 200.0

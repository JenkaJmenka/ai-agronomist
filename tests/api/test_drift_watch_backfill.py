"""Tests for backfill_drift_watch: idempotency and dry-run safety.

Uses in-memory SQLite with the ORM tables and a 20-week synthetic dataset
(hourly readings, stable sensor values) so the test never touches real data
and completes in a few seconds.

DRIFT_WATCH_MIN_HISTORY_WEEKS = 16; with step_weeks=4 and 20 weeks of hourly
data the calibration fires on the fifth (last) chunk and all subsequent inputs
are scored.  The test asserts idempotency and dry-run write-nothing behaviour
regardless of whether episodes are detected.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.drift_watch_service import backfill_drift_watch
from src.db.models import Base, DriftWatchEpisode, DriftWatchInput, RawReading

TENANT = "tenant_backfill_test"
GREENHOUSE = "gh_backfill_test"
# T0 is the first timestamp; 20 weeks of hourly readings gives 3360 rows.
# 20 weeks > DRIFT_WATCH_MIN_HISTORY_WEEKS (16) so calibration is reachable
# on the last 4-week chunk.
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
WEEKS = 20
N_HOURS = WEEKS * 7 * 24  # 3360 rows


def _make_readings(n: int) -> list[RawReading]:
    """Create n hourly RawReading rows with stable diurnal sensor values.

    Columns include measured_plant_temp (triggers temp_paired spec) and
    measured_hd (triggers hd_physics spec).  Values are sinusoidal but
    stable — no drift induced, so CUSUM should not fire under normal data.
    """
    rows = []
    for i in range(n):
        ts = T0 + dt.timedelta(hours=i)
        hour_phase = 2 * math.pi * (i % 24) / 24
        rows.append(
            RawReading(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                timestamp=ts,
                source="csv",
                data={
                    "temperature": 22.0 + 1.5 * math.sin(hour_phase),
                    "humidity": 65.0 + 3.0 * math.sin(hour_phase),
                    "co2": 400.0 + 15.0 * math.sin(hour_phase),
                    "light": max(0.0, 60.0 * math.sin(math.pi * ((i % 24) - 6) / 12)),
                    # plant temp = greenhouse temp minus small stable bias
                    "measured_plant_temp": 21.0 + 1.5 * math.sin(hour_phase),
                    # measured HD matches Tetens(T, RH) up to a stable bias
                    "measured_hd": 3.2 + 0.4 * math.sin(hour_phase),
                },
            )
        )
    return rows


@pytest_asyncio.fixture
async def db_with_data() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add_all(_make_readings(N_HOURS))
        await session.commit()
        yield session
    await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_backfill_dry_run_writes_nothing(db_with_data: AsyncSession) -> None:
    """dry_run=True must report span and specs but write zero rows."""
    summary = await backfill_drift_watch(
        db_with_data, TENANT, GREENHOUSE, step_weeks=4, dry_run=True
    )

    assert summary["dry_run"] is True
    assert summary["data_start"] is not None, "data_start must be set when data exists"
    assert summary["data_end"] is not None
    assert summary["episodes_recorded"] == 0
    # specs_would_be_built must be a non-empty list (we have plant_temp and hd cols)
    would_build = summary["specs_would_be_built"]
    assert isinstance(would_build, list)
    assert len(would_build) > 0, "Expected at least one spec from the synthetic data"
    # chunks_would_be_processed must be positive
    assert summary["chunks_would_be_processed"] > 0

    # No DriftWatchInput rows written.
    input_count = await db_with_data.scalar(
        select(func.count()).select_from(DriftWatchInput).where(
            DriftWatchInput.tenant_id == TENANT,
            DriftWatchInput.greenhouse_id == GREENHOUSE,
        )
    )
    assert input_count == 0, "dry_run must not insert DriftWatchInput rows"

    # No DriftWatchEpisode rows written.
    ep_count = await db_with_data.scalar(
        select(func.count()).select_from(DriftWatchEpisode).where(
            DriftWatchEpisode.tenant_id == TENANT,
            DriftWatchEpisode.greenhouse_id == GREENHOUSE,
        )
    )
    assert ep_count == 0, "dry_run must not insert DriftWatchEpisode rows"


async def test_backfill_idempotent(db_with_data: AsyncSession) -> None:
    """Running backfill twice must produce identical episode count and input status."""
    # First run.
    summary1 = await backfill_drift_watch(
        db_with_data, TENANT, GREENHOUSE, step_weeks=4
    )
    assert summary1["dry_run"] is False
    assert summary1["chunks_processed"] == WEEKS // 4, (
        f"Expected {WEEKS // 4} chunks, got {summary1['chunks_processed']}"
    )

    ep_count1 = summary1["episodes_recorded"]
    inputs_status1 = dict(summary1["inputs_status"])

    # Second run — must delete and re-build, ending in the same state.
    summary2 = await backfill_drift_watch(
        db_with_data, TENANT, GREENHOUSE, step_weeks=4
    )
    ep_count2 = summary2["episodes_recorded"]
    inputs_status2 = dict(summary2["inputs_status"])

    assert ep_count2 == ep_count1, (
        f"Episode count changed on second run: {ep_count1} → {ep_count2}"
    )
    assert inputs_status2 == inputs_status1, (
        f"inputs_status changed on second run:\n"
        f"  first:  {inputs_status1!r}\n"
        f"  second: {inputs_status2!r}"
    )
    assert summary2["chunks_processed"] == summary1["chunks_processed"]

    # Double-check with a direct DB count.
    db_ep_count = await db_with_data.scalar(
        select(func.count()).select_from(DriftWatchEpisode).where(
            DriftWatchEpisode.tenant_id == TENANT,
            DriftWatchEpisode.greenhouse_id == GREENHOUSE,
        )
    )
    assert db_ep_count == ep_count2, "DB episode count must match summary"


async def test_backfill_no_data_returns_empty_summary(db_with_data: AsyncSession) -> None:
    """Scope with no readings returns a summary with None data bounds and 0 episodes."""
    summary = await backfill_drift_watch(
        db_with_data, "nonexistent_tenant", "nonexistent_gh"
    )
    assert summary["data_start"] is None
    assert summary["data_end"] is None
    assert summary["episodes_recorded"] == 0
    assert summary["inputs_status"] == {}

"""Zero/all boundaries of collision-safe synthetic anomaly injection.

Split from ``test_synthetic_injection_collision.py`` for the 300-line
guardrail: that module pins the core skip/pairing/honest-count contract on a
partially colliding run, this one pins the ends of the range —

* no ``RawReading`` history at all (``has_raw_cols=False``), so the service
  builds no raw rows and the raw insert must be skipped rather than issued
  with an empty VALUES list;
* nothing collides, the regression direction: the payload must still report
  the full request;
* everything collides: zero written, no exception, arithmetic still holding;
* nothing requested, which is the only way to reach the empty-``sensor_rows``
  guard in ``persist_injection_results``.

Grid/window/seed constants and the seeders live in
``_synthetic_collision_fixtures.py``.
"""

from __future__ import annotations

import datetime
import random

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import RawReading, SensorReading
from tests.api._synthetic_collision_fixtures import (
    COLLIDING_INDEXES,
    COLLISION_SEED,
    GRID_STEP,
    PANEL_ANOMALIES,
    PANEL_TOTAL,
    REAL_SOURCE,
    TENANT,
    WINDOW_START,
    count_synthetic,
    inject_panel,
    real_sensor_values,
    seed_real_grid,
    seed_real_raw_grid,
    seed_real_sensor_grid,
)

_WINDOW_START_DT = datetime.datetime.fromisoformat(WINDOW_START)
_GRID_ROWS_PER_DAY = int(86400 / GRID_STEP.total_seconds())

# An interval that divides the 5-minute grid, so every candidate timestamp of
# an 8-row run lands on a seeded real row: 86400 / 8 == 10800s == 36 * 300s.
_ALL_COLLIDE_NORMAL = 5
_ALL_COLLIDE_ANOMALIES = 3
_ALL_COLLIDE_TOTAL = _ALL_COLLIDE_NORMAL + _ALL_COLLIDE_ANOMALIES


# ---------------------------------------------------------------------------
# Case 4 — has_raw_cols=False: collisions with no raw history at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_handle_collisions_when_no_raw_history_exists(
    db_session: AsyncSession,
) -> None:
    """No ``RawReading`` history means no learned columns and no raw rows built;
    the sensor-side counts must stay honest regardless."""
    await seed_real_sensor_grid(db_session)
    random.seed(COLLISION_SEED)

    summary = await inject_panel(db_session)

    assert summary["raw_injected"] == 0
    assert await count_synthetic(db_session, RawReading) == 0
    assert summary["skipped_collisions"] == len(COLLIDING_INDEXES)
    assert summary["injected"] == await count_synthetic(db_session, SensorReading)
    assert summary["anomalies"] + summary["normal"] == summary["injected"]


# ---------------------------------------------------------------------------
# Case 5 — regression: an uncollided run still reports the full request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_report_zero_skips_when_nothing_collides(
    db_session: AsyncSession,
) -> None:
    """Raw history seeded in the *previous* day, so column learning succeeds but
    no ``SensorReading`` exists anywhere in the injection window."""
    earlier_day = [
        _WINDOW_START_DT - (i + 1) * GRID_STEP for i in range(_GRID_ROWS_PER_DAY)
    ]
    await seed_real_raw_grid(db_session, earlier_day)
    random.seed(COLLISION_SEED)

    summary = await inject_panel(db_session)

    assert summary["skipped_collisions"] == 0
    assert summary["injected"] == summary["requested"] == PANEL_TOTAL
    assert summary["raw_injected"] == PANEL_TOTAL
    assert summary["anomalies"] == PANEL_ANOMALIES
    assert summary["anomalies"] + summary["normal"] == summary["injected"]


# ---------------------------------------------------------------------------
# Case 6 — every candidate timestamp already taken
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_insert_nothing_when_every_candidate_collides(
    db_session: AsyncSession,
) -> None:
    """An 8-row run over 24h spaces rows 10800s apart — a whole multiple of the
    300s grid — so all eight candidates sit on seeded real rows."""
    await seed_real_grid(db_session)
    random.seed(COLLISION_SEED)

    summary = await inject_panel(
        db_session, n_normal=_ALL_COLLIDE_NORMAL, n_anomalies=_ALL_COLLIDE_ANOMALIES
    )

    assert summary["injected"] == 0
    assert summary["raw_injected"] == 0
    assert summary["requested"] == _ALL_COLLIDE_TOTAL
    assert summary["skipped_collisions"] == _ALL_COLLIDE_TOTAL
    assert summary["anomalies"] == 0
    assert summary["normal"] == 0
    assert await count_synthetic(db_session, SensorReading) == 0
    assert await count_synthetic(db_session, RawReading) == 0


@pytest.mark.asyncio
async def test_should_not_disturb_any_real_row_when_everything_collides(
    db_session: AsyncSession,
) -> None:
    seeded = await seed_real_grid(db_session)
    random.seed(COLLISION_SEED)

    await inject_panel(
        db_session, n_normal=_ALL_COLLIDE_NORMAL, n_anomalies=_ALL_COLLIDE_ANOMALIES
    )

    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(SensorReading)
            .where(SensorReading.tenant_id == TENANT)
            .order_by(SensorReading.timestamp)
        )
    ).scalars().all()
    assert len(rows) == len(seeded)
    assert {row.source for row in rows} == {REAL_SOURCE}
    assert all(
        row.temperature == pytest.approx(real_sensor_values(i)["temperature"])
        for i, row in enumerate(rows)
    )


# ---------------------------------------------------------------------------
# Nothing requested — the empty-sensor_rows guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_write_nothing_and_report_zeros_when_no_rows_requested(
    db_session: AsyncSession,
) -> None:
    """``n_normal=0, n_anomalies=0`` builds no rows at all, so neither insert is
    issued.  Reaches the only branch in ``persist_injection_results`` that a
    non-empty run cannot: an empty VALUES list would be a SQL error."""
    await seed_real_grid(db_session)
    random.seed(COLLISION_SEED)

    summary = await inject_panel(db_session, n_normal=0, n_anomalies=0)

    assert summary["requested"] == 0
    assert summary["injected"] == 0
    assert summary["raw_injected"] == 0
    assert summary["skipped_collisions"] == 0
    assert summary["anomalies"] == 0
    assert summary["normal"] == 0
    assert await count_synthetic(db_session, SensorReading) == 0
    assert await count_synthetic(db_session, RawReading) == 0

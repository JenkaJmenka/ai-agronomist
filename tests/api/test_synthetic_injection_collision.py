"""Collision behaviour of synthetic anomaly injection against real replay data.

PR #90 anchored the injection window to the window the user is actually
viewing — dense real replay data on a 5-minute grid — so injected rows now
land on top of existing readings as a matter of course.  Before the fix in
``synthetic_anomaly_persistence.py`` that was a unique-constraint violation
that rolled back the whole transaction: HTTP 500, nothing written.

What this module pins:

* a colliding row is **skipped**, and the real reading underneath keeps its
  own values *and* its ``source`` — i.e. ``ON CONFLICT DO NOTHING``, never
  ``DO UPDATE``.  ``SensorReading.PROVENANCE_TRACKED`` is ``True``, so a
  ``DO UPDATE`` would apply the ADR-010 "last writer owns provenance" rule and
  let a synthetic row relabel a real reading;
* the two tables stay paired — no synthetic ``RawReading`` survives at a
  timestamp whose ``SensorReading`` was skipped, and both halves exist at a
  timestamp that did insert;
* the response payload reports rows **actually inserted**, never requested —
  including ``anomalies``, which can come back below the requested count when
  an anomaly position is the one that collided.

The zero/all boundaries live in
``test_synthetic_injection_collision_boundaries.py`` (split for the 300-line
guardrail); grid/window/seed constants and the dense-grid seeder live in
``_synthetic_collision_fixtures.py``.  The non-colliding injection contract
(validation, score bands, markers) is covered by
``test_synthetic_anomalies_service.py``.
"""

from __future__ import annotations

import random

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import RawReading, SensorReading
from tests.api._synthetic_collision_fixtures import (
    COLLIDING_INDEXES,
    COLLISION_SEED,
    PANEL_ANOMALIES,
    PANEL_TOTAL,
    REAL_SOURCE,
    candidate_timestamps,
    count_synthetic,
    count_synthetic_anomaly_rows,
    inject_panel,
    naive_utc,
    real_row_at,
    real_sensor_values,
    seed_real_grid,
    synthetic_raw_timestamps,
    synthetic_sensor_timestamps,
)


# ---------------------------------------------------------------------------
# Case 1 — a collision skips instead of erroring, and never overwrites
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_skip_colliding_rows_without_raising(db_session: AsyncSession) -> None:
    await seed_real_grid(db_session)
    random.seed(COLLISION_SEED)

    summary = await inject_panel(db_session)

    assert summary["skipped_collisions"] >= 1, (
        "the dense 5-minute grid must produce at least one collision, "
        "or this suite is not exercising the fix"
    )
    assert summary["injected"] == PANEL_TOTAL - summary["skipped_collisions"]


@pytest.mark.asyncio
async def test_should_leave_a_collided_real_reading_completely_untouched(
    db_session: AsyncSession,
) -> None:
    """The DO NOTHING vs DO UPDATE assertion: ``source`` must survive too.

    ``SensorReading.PROVENANCE_TRACKED`` means a ``DO UPDATE`` upsert would
    relabel this row's provenance to the synthetic marker and overwrite its
    measured values.  Asserting the values alone would not catch that.
    """
    seeded = await seed_real_grid(db_session)
    collided_ts = seeded[0]
    expected = real_sensor_values(0)
    random.seed(COLLISION_SEED)

    await inject_panel(db_session)

    row = await real_row_at(db_session, collided_ts)
    assert row is not None
    assert row.source == REAL_SOURCE, "a skipped synthetic row must not relabel provenance"
    assert row.anomaly_type is None
    assert row.temperature == pytest.approx(expected["temperature"])
    assert row.humidity == pytest.approx(expected["humidity"])
    assert row.co2 == pytest.approx(expected["co2"])
    assert row.light == pytest.approx(expected["light"])


# ---------------------------------------------------------------------------
# Case 2 — the two tables stay paired across a skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_write_no_orphan_raw_row_at_a_skipped_timestamp(
    db_session: AsyncSession,
) -> None:
    """Independent per-table DO NOTHING would leave a raw-only half-pair behind."""
    await seed_real_grid(db_session)
    random.seed(COLLISION_SEED)

    await inject_panel(db_session)

    candidates = candidate_timestamps()
    attempted = {naive_utc(ts) for ts in candidates}
    inserted = await synthetic_sensor_timestamps(db_session)
    skipped = attempted - inserted

    assert skipped == {naive_utc(candidates[i]) for i in COLLIDING_INDEXES}, (
        "collision set must be exactly the grid-aligned indexes; a different set "
        "means the interval arithmetic this suite assumes has changed"
    )
    raw_written = await synthetic_raw_timestamps(db_session)
    assert raw_written & skipped == set()


@pytest.mark.asyncio
async def test_should_write_both_halves_at_an_inserted_timestamp(
    db_session: AsyncSession,
) -> None:
    await seed_real_grid(db_session)
    random.seed(COLLISION_SEED)

    await inject_panel(db_session)

    inserted = await synthetic_sensor_timestamps(db_session)
    assert inserted, "guard: the run must have inserted something to pair"
    assert await synthetic_raw_timestamps(db_session) == inserted


# ---------------------------------------------------------------------------
# Case 3 — reported counts equal rows actually in the database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_report_counts_matching_rows_actually_inserted(
    db_session: AsyncSession,
) -> None:
    await seed_real_grid(db_session)
    random.seed(COLLISION_SEED)

    summary = await inject_panel(db_session)

    assert summary["requested"] == PANEL_TOTAL
    assert summary["injected"] == await count_synthetic(db_session, SensorReading)
    assert summary["raw_injected"] == await count_synthetic(db_session, RawReading)
    assert summary["injected"] < summary["requested"], (
        "guard: a run with no collisions would make this test pass vacuously"
    )


# ---------------------------------------------------------------------------
# Case 7 — an anomaly position collides, so `anomalies` under-reports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_report_fewer_anomalies_than_requested_when_one_collides(
    db_session: AsyncSession,
) -> None:
    """``anomaly_positions`` comes from ``random.sample``, so WHICH rows are
    anomalous depends on the seed.  Under ``COLLISION_SEED`` position 0 is
    sampled, and position 0 is grid-aligned — so one of the eight requested
    anomalies is skipped.  A payload echoing the requested count would say 8
    and be wrong; this is the case that distinguishes them.
    """
    await seed_real_grid(db_session)
    random.seed(COLLISION_SEED)

    summary = await inject_panel(db_session)

    assert summary["anomalies"] < PANEL_ANOMALIES
    assert summary["anomalies"] == await count_synthetic_anomaly_rows(db_session)
    assert summary["anomalies"] + summary["normal"] == summary["injected"]

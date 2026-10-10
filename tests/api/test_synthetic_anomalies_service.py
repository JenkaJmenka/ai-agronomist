"""Behavioural tests for synthetic anomaly *injection*.

Complements ``test_synthetic_anomalies_gas_isolation.py`` (which pins the
climate-``co2`` vs gas-panel-``gas_co2`` separation) by covering the injection
half of the service contract: request validation, window resolution, the
no-raw-data fallback, the score bands from ADR-005 §2.2, and the
``source='synthetic:<type>'`` provenance marker from ADR-010.

Removal semantics live in ``test_synthetic_anomalies_removal.py`` (split for
the 300-line guardrail); shared constants, the raw-history seeder and the row
builders live in ``tests/api/_synthetic_service_fixtures.py``.
"""

from __future__ import annotations

import datetime
import random

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.synthetic_anomalies import ANOMALY_TYPES, delete_synthetic_anomalies
from src.config import SYNTHETIC_ANOMALY_SCORE_RANGE, SYNTHETIC_CONTEXT_SCORE_RANGE
from src.db.models import RawReading, SensorReading
from tests.api._synthetic_service_fixtures import (
    DEFAULT_WINDOW_HOURS,
    GREENHOUSE,
    HISTORY_ROWS,
    RNG_SEED,
    TENANT,
    WINDOW_END,
    WINDOW_START,
    inject,
    raw_sources,
    seed_raw_history,
    sensor_rows_of,
)

_CONTEXT_LABEL = "synthetic_context"
_CONTEXT_MARKER = "synthetic:context"
_INJECTED_ROWS = 20


@pytest.fixture(autouse=True)
def seed_service_rng() -> None:
    random.seed(RNG_SEED)


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_reject_unknown_anomaly_type_on_inject(db_session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await inject(db_session, anomaly_type="tornado")
    assert exc_info.value.status_code == 422
    assert "tornado" in exc_info.value.detail


@pytest.mark.asyncio
async def test_should_reject_unparseable_window_datetime(db_session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await inject(db_session, window_start="not-a-date", window_end=WINDOW_END)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_should_reject_window_end_at_or_before_window_start(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await inject(db_session, window_start=WINDOW_END, window_end=WINDOW_START)
    assert exc_info.value.status_code == 422
    assert "window_end" in exc_info.value.detail


@pytest.mark.asyncio
async def test_should_treat_naive_window_bounds_as_utc(db_session: AsyncSession) -> None:
    summary = await inject(
        db_session, window_start="2026-04-05T00:00:00", window_end="2026-04-07T00:00:00"
    )
    assert summary["window_start"].endswith("+00:00")
    assert summary["window_hours"] == DEFAULT_WINDOW_HOURS


@pytest.mark.asyncio
async def test_should_default_to_the_last_48_hours_when_no_window_given(
    db_session: AsyncSession,
) -> None:
    summary = await inject(db_session, window_start=None, window_end=None)

    assert summary["window_hours"] == DEFAULT_WINDOW_HOURS
    window_end = datetime.datetime.fromisoformat(summary["window_end"])
    window_start = datetime.datetime.fromisoformat(summary["window_start"])
    assert window_end - window_start == datetime.timedelta(hours=DEFAULT_WINDOW_HOURS)
    assert abs((datetime.datetime.now(datetime.timezone.utc) - window_end).total_seconds()) < 60


# ---------------------------------------------------------------------------
# Column learning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_fall_back_to_canonical_columns_when_no_raw_history_exists(
    db_session: AsyncSession,
) -> None:
    summary = await inject(db_session)

    assert summary["injected"] == _INJECTED_ROWS
    assert summary["raw_injected"] == 0, "no learned columns means no raw rows are fabricated"
    assert set(summary["columns_used"]) == {"temperature", "humidity", "co2", "light"}


@pytest.mark.asyncio
async def test_should_ignore_non_numeric_raw_columns_when_learning_ranges(
    db_session: AsyncSession,
) -> None:
    await seed_raw_history(db_session, extra_columns={"device_status": "ok"})

    summary = await inject(db_session)

    assert "device_status" not in summary["columns_used"]
    assert "co2" in summary["columns_used"]


@pytest.mark.asyncio
async def test_should_mirror_learned_columns_into_injected_raw_rows(
    db_session: AsyncSession,
) -> None:
    await seed_raw_history(db_session)

    summary = await inject(db_session)

    assert summary["raw_injected"] == _INJECTED_ROWS
    rows = await db_session.execute(
        select(RawReading).where(RawReading.source.like("synthetic:%"))
    )
    injected = list(rows.scalars().all())
    assert injected
    assert all(set(row.data) == set(summary["columns_used"]) for row in injected)


# ---------------------------------------------------------------------------
# Score bands (ADR-005 §2.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("anomaly_type", ANOMALY_TYPES)
async def test_should_score_anomaly_rows_inside_the_canonical_synthetic_band(
    db_session: AsyncSession, anomaly_type: str
) -> None:
    band_lo, band_hi = SYNTHETIC_ANOMALY_SCORE_RANGE
    await inject(db_session, anomaly_type=anomaly_type)

    rows = await db_session.execute(
        select(SensorReading).where(SensorReading.anomaly_type == anomaly_type)
    )
    spike_rows = list(rows.scalars().all())
    assert len(spike_rows) == 10
    assert all(band_lo <= row.anomaly_score <= band_hi for row in spike_rows)
    assert all(row.score_scale == "synthetic" for row in spike_rows)
    assert all(row.anomaly_score_raw is None for row in spike_rows)


@pytest.mark.asyncio
async def test_should_score_context_rows_below_the_live_noise_floor(
    db_session: AsyncSession,
) -> None:
    context_lo, context_hi = SYNTHETIC_CONTEXT_SCORE_RANGE
    await inject(db_session)

    rows = await db_session.execute(
        select(SensorReading).where(SensorReading.anomaly_type == _CONTEXT_LABEL)
    )
    context_rows = list(rows.scalars().all())
    assert len(context_rows) == 10
    assert all(context_lo <= row.anomaly_score <= context_hi for row in context_rows)


# ---------------------------------------------------------------------------
# Provenance marker (ADR-010) — the write side
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("anomaly_type", ANOMALY_TYPES)
async def test_should_tag_every_injected_sensor_row_with_the_synthetic_source_marker(
    db_session: AsyncSession, anomaly_type: str
) -> None:
    """The marker write itself, asserted directly on rows read back from the DB.

    Why this exists: before it, the *only* test that failed if
    ``inject_synthetic_anomalies`` stopped writing ``SensorReading.source`` was
    ``test_synthetic_anomalies_gas_isolation.py::
    test_should_keep_gas_co2_quiet_for_every_targeted_anomaly_type`` — and it
    failed incidentally, with a unique-constraint ``IntegrityError``, because
    its per-iteration cleanup (keyed off the marker) left rows behind and the
    next iteration reused the same window timestamps.  The refactor its own
    comment invites (per-iteration timestamps) would have removed that
    accident and left the marker write with zero coverage and a green suite.
    """
    summary = await inject(db_session, anomaly_type=anomaly_type)
    assert summary["injected"] == _INJECTED_ROWS

    rows = await sensor_rows_of(db_session)
    assert len(rows) == _INJECTED_ROWS

    spike_sources = {row.source for row in rows if row.anomaly_type == anomaly_type}
    filler_sources = {row.source for row in rows if row.anomaly_type == _CONTEXT_LABEL}

    assert spike_sources == {f"synthetic:{anomaly_type}"}
    assert filler_sources == {_CONTEXT_MARKER}
    assert all(row.source is not None for row in rows), "no injected row may be untagged"


@pytest.mark.asyncio
async def test_should_tag_injected_rows_in_both_tables_with_the_same_marker(
    db_session: AsyncSession,
) -> None:
    """SensorReading and RawReading must agree, row for row, on the marker.

    ``delete_synthetic_anomalies`` issues two independent DELETEs keyed off the
    same ``synthetic:%`` predicate, one per table.  If the two writers ever
    drift apart, removal becomes partial — one table emptied, the other left
    holding injected rows.
    """
    await seed_raw_history(db_session)

    summary = await inject(db_session)

    sensor_sources = [row.source for row in await sensor_rows_of(db_session)]
    assert all(source is not None for source in sensor_sources), (
        "every injected sensor row must carry a marker before the two tables can be compared"
    )
    sensor_markers = sorted(sensor_sources)
    injected_raw_markers = sorted(
        source
        for source in await raw_sources(db_session)
        if source is not None and source.startswith("synthetic:")
    )

    assert len(sensor_markers) == summary["injected"]
    assert len(injected_raw_markers) == summary["raw_injected"]
    assert sensor_markers == injected_raw_markers


@pytest.mark.asyncio
@pytest.mark.parametrize("anomaly_type", ANOMALY_TYPES)
async def test_should_remove_every_row_a_real_injection_just_wrote(
    db_session: AsyncSession, anomaly_type: str
) -> None:
    """The writer↔reader round trip — production code on both sides.

    This is what the marker is *for*: whatever ``inject_synthetic_anomalies``
    writes, ``delete_synthetic_anomalies`` must be able to find.  Deliberately
    uses no hand-built rows and no test-side copy of the marker format (see
    ``source_marker_for``'s docstring), so changing the format in one of the
    two production call sites fails here instead of passing by agreeing with
    the test.
    """
    await seed_raw_history(db_session)
    summary = await inject(db_session, anomaly_type=anomaly_type)
    assert summary["injected"] == _INJECTED_ROWS, "guard: the round trip must have rows to remove"
    assert len(await sensor_rows_of(db_session)) == _INJECTED_ROWS

    result = await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
    )

    assert result["deleted_sensor"] == _INJECTED_ROWS
    assert await sensor_rows_of(db_session) == [], "every injected sensor row must be removable"
    assert await raw_sources(db_session) == ["csv"] * HISTORY_ROWS

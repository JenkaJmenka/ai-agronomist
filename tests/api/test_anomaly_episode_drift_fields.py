"""ADR-013 §4 fields on GET /api/anomalies/episodes for drift_watch episodes.

The fixtures hand-insert one flagged ``sensor_readings`` row per episode plus
the three drift tables, because the feed groups flagged readings and nothing in
the writer path creates such a reading (see the report on this task). The
service entry point is called directly so the SQL statements can be counted.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.schemas.anomaly_episodes import AnomalyEpisodeSummary
from src.api.services.anomaly_episode_service import fetch_anomaly_episodes
from src.db.models import (
    AnomalyEpisodeRecord,
    Base,
    DriftWatchEpisode,
    DriftWatchInput,
    SensorReading,
)

T, G = "t_drift_fields", "g_drift_fields"
UTC = dt.timezone.utc
DETECTED = dt.datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LAST = DETECTED + dt.timedelta(days=3)
WINDOW = (dt.datetime(2026, 5, 1, tzinfo=UTC), dt.datetime(2026, 7, 1, tzinfo=UTC))
SPEC = {
    "input_id": "temp_paired", "kind": "paired_probes",
    "target_channel": "temperature", "channels": ["measured_plant_temp"],
    "min_shift": 1.0,
}


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _flagged(ts: dt.datetime, detector: str, channel: str) -> SensorReading:
    return SensorReading(
        tenant_id=T, greenhouse_id=G, timestamp=ts, temperature=22.0,
        humidity=60.0, co2=400.0, light=300.0, anomaly_score=0.9,
        trigger_channel=channel, trigger_detector=detector,
        detected_severity="warning", trigger_value=-2.9,
    )


def _seed_drift(db: AsyncSession, offset: float, direction: str) -> None:
    db.add_all([
        _flagged(DETECTED, "drift_watch", "temperature"),
        AnomalyEpisodeRecord(
            id="ae1", tenant_id=T, greenhouse_id=G, channel="temperature",
            detector="drift_watch", fault_type="calibration_drift",
            onset_ts=DETECTED - dt.timedelta(hours=5), end_ts=LAST,
            detected_ts=DETECTED, duration_seconds=1, severity="warning",
            trigger_value=offset, is_ongoing=True, source=None,
        ),
        DriftWatchEpisode(
            tenant_id=T, greenhouse_id=G, input_id="temp_paired",
            episode_id="temp_paired_1", target_channel="temperature",
            direction=direction, first_detected_ts=DETECTED,
            last_confirmed_ts=LAST, offset_robust=offset,
            input_status_at_detection="shadow",
        ),
        DriftWatchInput(
            tenant_id=T, greenhouse_id=G, input_id="temp_paired",
            spec_json=json.dumps(SPEC), status="shadow",
        ),
    ])


async def _fetch(db: AsyncSession) -> list[dict]:
    await db.commit()
    return await fetch_anomaly_episodes(
        db, tenant_id=T, greenhouse_id=G, start=WINDOW[0], end=WINDOW[1],
        threshold=0.5, sample_limit=5, incidents=[],
    )


@pytest.mark.asyncio
async def test_drift_episode_carries_signed_offset_input_kind_and_message(db):
    # A "-" episode: the robust offset is NEGATIVE (trigger_value keeps the sign).
    _seed_drift(db, offset=-2.94, direction="-")
    (ep,) = await _fetch(db)
    out = AnomalyEpisodeSummary.model_validate(ep)
    assert out.detector == "drift_watch"
    assert out.drift_offset == pytest.approx(-2.94)
    assert out.drift_input_id == "temp_paired"
    assert out.drift_input_kind == "paired_probes"
    assert out.drift_first_detected_ts == "2026-06-01T12:00:00+00:00"
    assert out.drift_message == (
        "Greenhouse air temperature and plant temperature disagree by 2.9 °C. "
        "One of the two probes may have drifted."
    )


@pytest.mark.asyncio
async def test_non_drift_episode_has_every_drift_field_null(db):
    db.add(_flagged(DETECTED, "statistical", "humidity"))
    (ep,) = await _fetch(db)
    out = AnomalyEpisodeSummary.model_validate(ep)
    assert out.detector == "statistical"
    assert (out.drift_offset, out.drift_input_id, out.drift_first_detected_ts,
            out.drift_input_kind, out.drift_message) == (None,) * 5


@pytest.mark.asyncio
async def test_drift_fields_cost_one_extra_query_per_page_not_per_row(db):
    # Two drift episodes on two channels + one non-drift: still ONE extra query.
    _seed_drift(db, offset=-2.94, direction="-")
    db.add(_flagged(DETECTED + dt.timedelta(days=10), "drift_watch", "co2"))
    db.add(_flagged(DETECTED + dt.timedelta(days=20), "statistical", "humidity"))
    await db.commit()

    seen: list[str] = []
    sync_engine = db.bind.sync_engine
    listener = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
    event.listen(sync_engine, "before_cursor_execute", listener)
    try:
        baseline = await fetch_anomaly_episodes(
            db, tenant_id=T, greenhouse_id=G, start=WINDOW[0], end=WINDOW[1],
            threshold=0.5, sample_limit=5, incidents=[],
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", listener)
    assert len(baseline) == 3
    drift_queries = [s for s in seen if "drift_watch_episodes" in s]
    assert len(drift_queries) == 1
    # flagged-row query + the pre-existing recovery-release query + the one
    # drift join query: nothing per row.
    assert len(seen) == 3

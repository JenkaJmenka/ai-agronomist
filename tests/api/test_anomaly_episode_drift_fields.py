"""Drift-watch alerts in GET /api/anomalies/episodes (ADR-013 section 4).

Drift alerts reach the feed straight from ``anomaly_episodes``, with the two
drift tables supplying input id and kind. No flagged reading is fabricated.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.api.schemas.anomaly_episodes import AnomalyEpisodesResponse
from src.api.services.anomaly_episode_service import fetch_anomaly_episodes
from src.db.models import (
    AnomalyEpisodeRecord,
    Base,
    DriftWatchEpisode,
    DriftWatchInput,
    SensorReading,
)
from src.db.session import get_db

T, G = "t_drift_fields", "g_drift_fields"
UTC = dt.timezone.utc
DETECTED = dt.datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LAST = DETECTED + dt.timedelta(days=3)
READING_TS = DETECTED + dt.timedelta(days=9)
WINDOW = (dt.datetime(2026, 5, 1, tzinfo=UTC), dt.datetime(2026, 7, 1, tzinfo=UTC))
SPEC = {
    "input_id": "temp_paired", "kind": "paired_probes",
    "target_channel": "temperature", "channels": ["measured_plant_temp"],
    "min_shift": 1.0,
}


def _alert(rid: str, *, tenant: str = T, source: str | None = None,
           shift_days: int = 0) -> AnomalyEpisodeRecord:
    detected = DETECTED + dt.timedelta(days=shift_days)
    return AnomalyEpisodeRecord(
        id=rid, tenant_id=tenant, greenhouse_id=G, channel="temperature",
        detector="drift_watch", fault_type="calibration_drift",
        onset_ts=detected - dt.timedelta(hours=5), end_ts=LAST + dt.timedelta(days=shift_days),
        detected_ts=detected, duration_seconds=1, severity="warning",
        trigger_value=-2.94, is_ongoing=True, source=source,
    )


def _reading() -> SensorReading:
    return SensorReading(
        tenant_id=T, greenhouse_id=G, timestamp=READING_TS, temperature=22.0,
        humidity=60.0, co2=400.0, light=300.0, anomaly_score=0.9,
        trigger_channel="humidity", trigger_detector="statistical",
        detected_severity="warning", trigger_value=30.0,
    )


@pytest_asyncio.fixture
async def db():
    """One reading episode plus one drift alert ('-', offset -2.94) for T."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        s.add_all([
            _alert("ae1"),
            DriftWatchEpisode(
                tenant_id=T, greenhouse_id=G, input_id="temp_paired",
                episode_id="temp_paired_1", target_channel="temperature",
                direction="-", first_detected_ts=DETECTED, last_confirmed_ts=LAST,
                offset_robust=-2.94, input_status_at_detection="shadow",
            ),
            DriftWatchInput(
                tenant_id=T, greenhouse_id=G, input_id="temp_paired",
                spec_json=json.dumps(SPEC), status="shadow",
            ),
            _reading(),
        ])
        await s.commit()
        yield s
    await engine.dispose()


@pytest.fixture
def client(db):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_drift_alert_is_its_own_feed_episode_with_exact_fields_and_counts(client):
    r = client.get("/api/anomalies/episodes", params={
        "tenant_id": T, "greenhouse_id": G, "threshold": 0.5,
        "start": WINDOW[0].isoformat(), "end": WINDOW[1].isoformat(),
    })
    assert r.status_code == 200
    body = AnomalyEpisodesResponse.model_validate(r.json())
    by_det = {e.detector: e for e in body.episodes}
    drift, reading = by_det["drift_watch"], by_det["statistical"]
    assert drift.episode_id == "drift_watch:ae1"
    assert (drift.start_ts, drift.end_ts) == (
        "2026-06-01T12:00:00+00:00", "2026-06-04T12:00:00+00:00")
    assert (drift.severity, drift.sample_count, drift.samples) == ("warning", 0, [])
    assert drift.drift_offset == pytest.approx(-2.94)  # signed: "-" episode
    assert drift.drift_input_id == "temp_paired"
    assert drift.drift_input_kind == "paired_probes"
    assert drift.drift_first_detected_ts == "2026-06-01T12:00:00+00:00"
    assert drift.headline == drift.drift_message == (
        "Greenhouse air temperature and plant temperature disagree by 2.9 °C. "
        "One of the two probes may have drifted."
    )
    # episode_count includes the alert; anomaly_count counts flagged READINGS
    # only (1), so a drift alert never moves it.
    assert body.episode_count == 2
    assert body.anomaly_count == 1
    # The reading episode is unchanged: every drift field is null.
    assert (reading.drift_offset, reading.drift_input_id, reading.drift_input_kind,
            reading.drift_first_detected_ts, reading.drift_message) == (None,) * 5


@pytest.mark.asyncio
async def test_other_tenant_synthetic_and_out_of_window_alerts_are_excluded(db):
    db.add_all([
        _alert("other_tenant", tenant="someone_else"),
        _alert("synthetic", source="synthetic:drift", shift_days=1),
    ])
    await db.commit()
    kw = dict(tenant_id=T, greenhouse_id=G, threshold=0.5, sample_limit=5, incidents=[])
    eps = await fetch_anomaly_episodes(db, start=WINDOW[0], end=WINDOW[1], **kw)
    assert sorted(e["episode_id"] for e in eps if e["detector"] == "drift_watch") == [
        "drift_watch:ae1"]
    later = await fetch_anomaly_episodes(
        db, start=LAST + dt.timedelta(days=1), end=WINDOW[1], **kw)
    assert not [e for e in later if e["detector"] == "drift_watch"]


@pytest.mark.asyncio
async def test_exactly_one_drift_query_per_page(db):
    db.add(_alert("ae2", shift_days=2))  # a second alert must not add a query
    await db.commit()
    seen: list[str] = []
    eng = db.bind.sync_engine
    listener = lambda conn, cur, stmt, *a: seen.append(stmt)  # noqa: E731
    event.listen(eng, "before_cursor_execute", listener)
    try:
        eps = await fetch_anomaly_episodes(
            db, tenant_id=T, greenhouse_id=G, start=WINDOW[0], end=WINDOW[1],
            threshold=0.5, sample_limit=5, incidents=[])
    finally:
        event.remove(eng, "before_cursor_execute", listener)
    assert sum(e["detector"] == "drift_watch" for e in eps) == 2
    assert len([s for s in seen if "drift_watch_episodes" in s]) == 1
    # flagged-row query + existing recovery-release query + the drift join.
    assert len(seen) == 3

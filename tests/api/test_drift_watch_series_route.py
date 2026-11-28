"""GET /api/drift-watch/inputs/{tenant}/{gh}/{input}/series (read-only replay).

Reuses the alert-writer scope: 36 weeks of paired-probe data whose plant probe
reads 3 C low for the last 10 days (a +3 C step in ``dev``).
"""

from __future__ import annotations

import datetime as dt
from typing import Generator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.auth import clear_test_api_keys, register_test_api_key
from src.api.main import app
from src.db.models import Base, DriftWatchEpisode, DriftWatchFeedback, DriftWatchInput
from src.db.session import get_db
from tests.api.test_drift_watch_alert_writer import DATA_END, G, N, T, _rows, _upload

KEY = {"X-API-Key": "secret-series"}
URL = f"/api/drift-watch/inputs/{T}/{G}/temp_paired/series"
STEP_START = DATA_END - dt.timedelta(hours=24 * 10 - 2)


@pytest_asyncio.fixture
async def seeded() -> Generator[AsyncSession, None, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add_all(_rows())
        await s.commit()
        await _upload(s)
        ep = (await s.execute(select(DriftWatchEpisode))).scalars().first()
        assert ep is not None
        s.add(DriftWatchFeedback(
            tenant_id=T, greenhouse_id=G, input_id="temp_paired", direction=ep.direction,
            first_detected_ts=ep.first_detected_ts, label="confirmed_drift",
            created_at=dt.datetime.now(dt.timezone.utc),
        ))
        await s.commit()
        yield s
    await engine.dispose()


@pytest.fixture
def client(seeded: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    async def override():
        yield seeded

    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
    clear_test_api_keys()
    register_test_api_key(T, "secret-series")
    register_test_api_key("other_tenant", "secret-other")
    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    clear_test_api_keys()


def test_series_shows_step_label_and_matches_stored_episode(client: TestClient) -> None:
    # The first upload scored only its 10-week context, so a cold replay finds a
    # genuine extra alarm on 2026-06-30 that the stored state never saw; the
    # window starts after it.
    body = client.get(URL, params={"start": "2026-07-15T00:00:00Z"}, headers=KEY).json()
    assert body["unit"] == "°C" and body["kind"] == "paired_probes"
    assert body["alert_floor"] == 2.0 * body["min_shift"]
    assert body["calibration_window_end"] is not None and body["h"] > 0
    pts = body["points"]
    after = [p["dev"] for p in pts if p["ts"] >= STEP_START.isoformat().replace("+00:00", "")][-100:]
    assert abs(sum(after) / len(after) - 3.0) < 0.5 or abs(sum(after) / len(after) + 3.0) < 0.5
    assert body["episodes"] and body["episodes"][0]["label"] == "confirmed_drift"
    assert body["replay_mismatch"] == 0


def test_series_tenant_and_span_guards(client: TestClient) -> None:
    other = {"X-API-Key": "secret-other"}
    assert client.get(URL, headers=other).status_code == 403
    r = client.get(URL, params={"start": "2025-01-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"}, headers=KEY)
    assert r.status_code == 422
    assert client.get(URL.replace("temp_paired", "nope"), headers=KEY).status_code == 404


@pytest.mark.asyncio
async def test_default_window_is_13_weeks_ending_at_last_processed(
    client: TestClient, seeded: AsyncSession
) -> None:
    before = (await seeded.execute(select(func.count()).select_from(DriftWatchInput))).scalar_one()
    body = client.get(URL, headers=KEY).json()
    end = dt.datetime.fromisoformat(body["window_end"].replace("Z", "+00:00"))
    start = dt.datetime.fromisoformat(body["window_start"].replace("Z", "+00:00"))
    assert end - start == dt.timedelta(weeks=13)
    assert end.replace(tzinfo=dt.timezone.utc) == DATA_END
    assert 0 < len(body["points"]) <= 13 * 168 + 1
    assert (await seeded.execute(select(func.count()).select_from(DriftWatchInput))).scalar_one() == before

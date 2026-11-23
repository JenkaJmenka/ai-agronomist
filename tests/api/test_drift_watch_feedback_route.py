"""POST /api/drift-watch/feedback and the new GET /inputs fields."""

from __future__ import annotations

import datetime as dt
import json
from typing import Generator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.auth import clear_test_api_keys, register_test_api_key
from src.api.main import app
from src.config import DRIFT_WATCH_ALERT_ENABLE_MAX_COUNT
from src.db.models import Base, DriftWatchEpisode, DriftWatchFeedback, DriftWatchInput
from src.db.session import get_db

URL = "/api/drift-watch/feedback"
TS = dt.datetime(2026, 5, 1, 3, tzinfo=dt.timezone.utc)
KEY_A = {"X-API-Key": "secret-a"}


def _body(**kw):
    b = {
        "tenant_id": "tenant_a", "greenhouse_id": "gh_1", "input_id": "inp_a",
        "direction": "+", "first_detected_ts": TS.isoformat(), "label": "false_alarm",
    }
    b.update(kw)
    return b


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add_all([
            DriftWatchInput(
                tenant_id="tenant_a", greenhouse_id="gh_1", input_id="inp_a",
                spec_json=json.dumps({"kind": "paired_probes", "target_channel": "temperature"}),
                status="armed",
            ),
            DriftWatchEpisode(
                tenant_id="tenant_a", greenhouse_id="gh_1", input_id="inp_a",
                episode_id="inp_a_0", target_channel="temperature", direction="+",
                first_detected_ts=TS, last_confirmed_ts=TS, input_status_at_detection="armed",
            ),
        ])
        await s.commit()
        yield s
    await engine.dispose()


@pytest.fixture
def client(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    async def override():
        yield db_session

    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
    clear_test_api_keys()
    register_test_api_key("tenant_a", "secret-a")
    register_test_api_key("tenant_b", "secret-b")
    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    clear_test_api_keys()


def test_feedback_upsert_and_episode_label(client: TestClient) -> None:
    r = client.post(URL, json=_body(), headers=KEY_A)
    assert r.status_code == 200 and r.json()["label"] == "false_alarm"
    r = client.post(URL, json=_body(label="confirmed_drift", note="probe checked"), headers=KEY_A)
    assert r.status_code == 200 and r.json()["label"] == "confirmed_drift"
    eps = client.get("/api/drift-watch/episodes", params={"tenant_id": "tenant_a"}, headers=KEY_A).json()
    assert [e["label"] for e in eps] == ["confirmed_drift"]
    # An upsert: re-posting an earlier label still leaves exactly one row (a
    # duplicate would make the unique key raise on the second POST).
    r = client.post(URL, json=_body(label="false_alarm"), headers=KEY_A)
    assert r.status_code == 200 and r.json()["label"] == "false_alarm"
    eps = client.get("/api/drift-watch/episodes", params={"tenant_id": "tenant_a"}, headers=KEY_A).json()
    assert [e["label"] for e in eps] == ["false_alarm"]


def test_feedback_unknown_episode_404(client: TestClient) -> None:
    r = client.post(URL, json=_body(direction="-"), headers=KEY_A)
    assert r.status_code == 404


def test_feedback_other_tenant_rejected_and_bad_label_422(client: TestClient) -> None:
    assert client.post(URL, json=_body(tenant_id="tenant_b"), headers=KEY_A).status_code == 403
    assert client.post(URL, json=_body()).status_code == 401
    assert client.post(URL, json=_body(label="maybe"), headers=KEY_A).status_code == 422


def test_inputs_expose_alert_fields(client: TestClient) -> None:
    r = client.get("/api/drift-watch/inputs", params={"tenant_id": "tenant_a"}, headers=KEY_A)
    assert r.status_code == 200
    x = r.json()[0]
    assert x["alert_state"] == "off"
    assert x["budget"] == DRIFT_WATCH_ALERT_ENABLE_MAX_COUNT
    assert x["oos_count"] is None and x["raw_count"] is None

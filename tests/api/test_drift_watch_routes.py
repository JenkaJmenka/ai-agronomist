"""Tenant-isolation tests for the drift-watch read routes.

Uses real authentication (registered test API keys, ``AUTH_ENABLED`` on) —
the auth dependency is never overridden. The reference status codes for
cross-tenant and unauthenticated requests are measured from the incidents
route, which uses the same ``require_api_key`` + ``enforce_tenant_access``.
"""

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
from src.db.models import Base, DriftWatchEpisode, DriftWatchInput
from src.db.session import get_db

INPUTS = "/api/drift-watch/inputs"
EPISODES = "/api/drift-watch/episodes"
REFERENCE = "/api/incidents/statistics"  # same shared access check

T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _input(tenant: str, input_id: str) -> DriftWatchInput:
    return DriftWatchInput(
        tenant_id=tenant,
        greenhouse_id="gh_1",
        input_id=input_id,
        spec_json=json.dumps({"kind": "paired_probes", "target_channel": "temperature"}),
        calibration_json=json.dumps({"measured_fa_per_month": 0.5, "self_test": []}),
        state_json=json.dumps({"secret": "state"}),
        status="armed",
    )


def _episode(tenant: str, input_id: str, episode_id: str, hours: int) -> DriftWatchEpisode:
    ts = T0 + dt.timedelta(hours=hours)
    return DriftWatchEpisode(
        tenant_id=tenant,
        greenhouse_id="gh_1",
        input_id=input_id,
        episode_id=episode_id,
        target_channel="temperature",
        direction="up",
        first_detected_ts=ts,
        last_confirmed_ts=ts + dt.timedelta(hours=1),
        onset_estimate_ts=ts,
        offset_estimate=0.7,
        peak_statistic=5.0,
        input_status_at_detection="armed",
    )


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                _input("tenant_a", "inp_a"),
                _episode("tenant_a", "inp_a", "ep_a1", 0),
                _episode("tenant_a", "inp_a", "ep_a2", 10),
                _input("tenant_b", "inp_b"),
                _episode("tenant_b", "inp_b", "ep_b1", 5),
            ]
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def client(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> Generator[TestClient, None, None]:
    async def override_get_db():
        yield db_session

    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
    clear_test_api_keys()
    register_test_api_key("tenant_a", "secret-a")
    register_test_api_key("tenant_b", "secret-b")
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    clear_test_api_keys()


KEY_A = {"X-API-Key": "secret-a"}


def test_tenant_a_sees_only_own_rows(client: TestClient) -> None:
    r = client.get(INPUTS, params={"tenant_id": "tenant_a"}, headers=KEY_A)
    assert r.status_code == 200
    assert [x["input_id"] for x in r.json()] == ["inp_a"]

    r = client.get(EPISODES, params={"tenant_id": "tenant_a"}, headers=KEY_A)
    assert r.status_code == 200
    assert sorted(x["episode_id"] for x in r.json()) == ["ep_a1", "ep_a2"]


@pytest.mark.parametrize("path", [INPUTS, EPISODES])
def test_cross_tenant_rejected_like_incidents(client: TestClient, path: str) -> None:
    expected = client.get(REFERENCE, params={"tenant_id": "tenant_b"}, headers=KEY_A).status_code
    assert expected in (401, 403)
    r = client.get(path, params={"tenant_id": "tenant_b"}, headers=KEY_A)
    assert r.status_code == expected
    assert "inp_b" not in r.text and "ep_b1" not in r.text


@pytest.mark.parametrize("path", [INPUTS, EPISODES])
def test_unauthenticated_rejected_like_incidents(client: TestClient, path: str) -> None:
    expected = client.get(REFERENCE, params={"tenant_id": "tenant_a"}).status_code
    assert expected == 401
    r = client.get(path, params={"tenant_id": "tenant_a"})
    assert r.status_code == expected
    assert "inp_a" not in r.text and "ep_a1" not in r.text


def test_limit_bounds_and_validation(client: TestClient) -> None:
    r = client.get(EPISODES, params={"tenant_id": "tenant_a", "limit": 1}, headers=KEY_A)
    assert r.status_code == 200
    assert [x["episode_id"] for x in r.json()] == ["ep_a2"]  # newest first
    for bad in (0, 201):
        r = client.get(EPISODES, params={"tenant_id": "tenant_a", "limit": bad}, headers=KEY_A)
        assert r.status_code == 422


def test_inputs_never_expose_raw_json_blobs(client: TestClient) -> None:
    r = client.get(INPUTS, params={"tenant_id": "tenant_a"}, headers=KEY_A)
    assert r.status_code == 200 and r.json()
    for row in r.json():
        assert not {"spec_json", "calibration_json", "state_json"} & set(row)
    assert "spec_json" not in r.text and "state_json" not in r.text

"""Admin enable/disable of drift-watch alerts, gated by the ``admin`` scope."""

from __future__ import annotations

import json
import logging
from typing import Generator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.auth import (
    ADMIN_SCOPE,
    clear_test_api_keys,
    register_test_api_key,
    register_test_web_api_key,
)
from src.api.main import app
from src.db.models import Base, DriftWatchInput
from src.db.session import get_db

ADMIN = {"X-API-Key": "admin-a"}
PLAIN = {"X-API-Key": "plain-a"}
OTHER_ADMIN = {"X-API-Key": "admin-b"}


def _url(action: str, input_id: str = "inp_elig", tenant: str = "tenant_a") -> str:
    return f"/api/drift-watch/inputs/{tenant}/gh_1/{input_id}/alerts/{action}"


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        for input_id, state in (
            ("inp_elig", "eligible"),
            ("inp_off", "off"),
            ("inp_on", "enabled"),
        ):
            s.add(
                DriftWatchInput(
                    tenant_id="tenant_a", greenhouse_id="gh_1", input_id=input_id,
                    spec_json=json.dumps({"kind": "paired_probes", "target_channel": "temperature"}),
                    status="armed", alert_state=state,
                )
            )
        await s.commit()
        yield s
    await engine.dispose()


@pytest.fixture
def client(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    async def override():
        yield db_session

    monkeypatch.setattr("src.config.AUTH_ENABLED", True)
    monkeypatch.setattr("src.api.auth.AUTH_ENABLED", True)
    monkeypatch.setattr("src.config.DRIFT_WATCH_ALERTS_ENABLED", True)
    clear_test_api_keys()
    register_test_api_key("tenant_a", "admin-a", name="ops-admin", scopes=frozenset({ADMIN_SCOPE}))
    register_test_api_key("tenant_a", "plain-a", name="plain")
    register_test_api_key("tenant_b", "admin-b", name="other-admin", scopes=frozenset({ADMIN_SCOPE}))
    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    clear_test_api_keys()


async def _state(db: AsyncSession, input_id: str) -> str:
    db.expire_all()
    return (
        await db.execute(select(DriftWatchInput.alert_state).where(DriftWatchInput.input_id == input_id))
    ).scalar_one()


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_401_without_key(client: TestClient, action: str) -> None:
    assert client.post(_url(action)).status_code == 401


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_403_key_without_admin_scope(client: TestClient, action: str) -> None:
    r = client.post(_url(action), headers=PLAIN)
    assert r.status_code == 403
    assert ADMIN_SCOPE in r.json()["message"]


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_403_cross_tenant(client: TestClient, action: str) -> None:
    assert client.post(_url(action), headers=OTHER_ADMIN).status_code == 403


@pytest.mark.asyncio
async def test_409_when_not_eligible(client: TestClient, db_session: AsyncSession) -> None:
    r = client.post(_url("enable", "inp_off"), headers=ADMIN)
    assert r.status_code == 409
    assert "not 'eligible'" in r.json()["message"]
    assert await _state(db_session, "inp_off") == "off"


@pytest.mark.asyncio
async def test_409_when_flag_off(
    client: TestClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.config.DRIFT_WATCH_ALERTS_ENABLED", False)
    r = client.post(_url("enable"), headers=ADMIN)
    assert r.status_code == 409
    assert "disabled globally" in r.json()["message"]
    assert await _state(db_session, "inp_elig") == "eligible"


@pytest.mark.asyncio
async def test_enable_from_eligible(
    client: TestClient, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="src.api.routes.drift_watch"):
        r = client.post(_url("enable"), headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["alert_state"] == "enabled"
    assert body["alert_state_reason"] == "enabled by operator"
    assert await _state(db_session, "inp_elig") == "enabled"
    msgs = [m for m in caplog.messages if "drift-watch alert enabled" in m]
    assert len(msgs) == 1 and "ops-admin" in msgs[0] and "inp_elig" in msgs[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("input_id", ["inp_elig", "inp_off", "inp_on"])
async def test_disable_from_any_state(
    client: TestClient, db_session: AsyncSession, input_id: str
) -> None:
    r = client.post(_url("disable", input_id), headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["alert_state"] == "off"
    assert r.json()["alert_state_reason"] == "disabled by operator"
    assert await _state(db_session, input_id) == "off"


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_404_unknown_input(client: TestClient, action: str) -> None:
    assert client.post(_url(action, "nope"), headers=ADMIN).status_code == 404


def test_web_credential_with_scope_and_tenant_header(client: TestClient) -> None:
    register_test_web_api_key("web-admin", scopes=frozenset({ADMIN_SCOPE}))
    headers = {"X-API-Key": "web-admin", "X-Tenant-Id": "tenant_a"}
    assert client.post(_url("disable"), headers=headers).status_code == 200


def test_existing_routes_accept_key_without_scopes(client: TestClient) -> None:
    r = client.get("/api/drift-watch/inputs", params={"tenant_id": "tenant_a"}, headers=PLAIN)
    assert r.status_code == 200

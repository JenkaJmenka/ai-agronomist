"""Route tests for GET /api/jobs/{job_id}/events (G5 SSE stream)."""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.db.jobs import create_job, set_job_result
from src.db.models import Base
from src.db.session import get_db


@pytest_asyncio.fixture
async def test_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def test_client(test_database):
    async def override_get_db():
        yield test_database

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _fake_job(status_: str, completed: int, total: int) -> SimpleNamespace:
    now = datetime.datetime.now(datetime.timezone.utc)
    return SimpleNamespace(
        id="job-1",
        tenant_id="tenant_1",
        greenhouse_id="default",
        job_type="upload_csv",
        status=status_,
        total_portions=total,
        completed_portions=completed,
        failed_portions=0,
        result=None,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=None,
    )


def _data_events(body: str) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_stream_404_for_unknown_job(test_client):
    response = test_client.get("/api/jobs/does-not-exist/events")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_terminal_job_emits_single_event_and_closes(test_client, test_database):
    job = await create_job(
        test_database, tenant_id="tenant_1", job_type="upload_csv"
    )
    await set_job_result(test_database, job.id, status="completed")

    response = test_client.get(f"/api/jobs/{job.id}/events")
    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    events = _data_events(response.text)
    assert len(events) == 1
    assert events[0]["status"] == "completed"
    assert events[0]["progress_pct"] == pytest.approx(100.0)
    assert set(events[0]) == {
        "status",
        "progress_pct",
        "completed_portions",
        "total_portions",
    }


def test_stream_emits_progress_then_terminal(test_client, monkeypatch):
    monkeypatch.setattr("src.api.routes.jobs.JOB_EVENTS_POLL_SECONDS", 0.0)
    polled = [
        _fake_job("running", 1, 4),  # route-level 404 guard
        _fake_job("running", 1, 4),
        _fake_job("running", 2, 4),
        _fake_job("completed", 4, 4),
    ]
    with patch("src.api.routes.jobs.get_job", new=AsyncMock(side_effect=polled)):
        response = test_client.get("/api/jobs/job-1/events")
    events = _data_events(response.text)
    assert [e["status"] for e in events] == ["running", "running", "completed"]
    assert [e["progress_pct"] for e in events] == [
        pytest.approx(25.0),
        pytest.approx(50.0),
        pytest.approx(100.0),
    ]


def test_stream_sends_keepalive_comment_when_unchanged(test_client, monkeypatch):
    monkeypatch.setattr("src.api.routes.jobs.JOB_EVENTS_POLL_SECONDS", 0.0)
    polled = [
        _fake_job("running", 1, 4),  # route-level 404 guard
        _fake_job("running", 1, 4),
        _fake_job("running", 1, 4),  # unchanged → keep-alive comment
        _fake_job("completed", 4, 4),
    ]
    with patch("src.api.routes.jobs.get_job", new=AsyncMock(side_effect=polled)):
        response = test_client.get("/api/jobs/job-1/events")
    assert ": keep-alive" in response.text
    events = _data_events(response.text)
    assert [e["status"] for e in events] == ["running", "completed"]


def test_stream_closes_at_deadline_without_terminal_status(test_client, monkeypatch):
    monkeypatch.setattr("src.api.routes.jobs.JOB_EVENTS_POLL_SECONDS", 0.0)
    monkeypatch.setattr("src.api.routes.jobs.JOB_EVENTS_MAX_SECONDS", 0.0)
    polled = [
        _fake_job("running", 1, 4),  # route-level 404 guard
        _fake_job("running", 1, 4),
    ]
    with patch("src.api.routes.jobs.get_job", new=AsyncMock(side_effect=polled)):
        response = test_client.get("/api/jobs/job-1/events")
    events = _data_events(response.text)
    assert len(events) == 1
    assert events[0]["status"] == "running"

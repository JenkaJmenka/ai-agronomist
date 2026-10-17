"""Acceptance: the deploy-skew hazard — newer code, older schema.

Only the API service runs ``alembic upgrade head``
(``docker/start-api.sh:21``), so a failed API deploy leaves the worker (and,
after a rollback, the API itself) running 028-aware code against a database
without ``live_scope_state``.  The required behaviour is degrade, never
crash: boot comes up with every scope idle, and the pause/resume endpoints
still act in-process while reporting ``desired_state_persisted: false``.

Backend proved the two service functions tolerate the missing table.  These
cases prove the *process* does: the real ``lifespan`` is run against a
schema built without that one table.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.auth import clear_test_api_keys
from src.api.main import app
from src.api.services import live_scope_state_service
from src.api.services.live_scope_state_service import (
    DesiredScopeState,
    fetch_desired_state,
)
from src.db.models import RawReading, SensorReading
from src.db.session import get_db
from src.live.scope_control import clear_all_suppressed_scopes
from tests.api.live_boot_harness import (
    GREENHOUSE,
    IDLE_WATCH_SECONDS,
    TENANT,
    LiveBootHarness,
    booted_harness,
)


@pytest.fixture(autouse=True)
def _reset_process_state() -> None:
    clear_all_suppressed_scopes()
    clear_test_api_keys()
    yield
    clear_all_suppressed_scopes()
    clear_test_api_keys()
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def unmigrated_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> AsyncGenerator[LiveBootHarness, None]:
    """Every production table except ``live_scope_state``."""
    async with booted_harness(
        monkeypatch, tmp_path, include_scope_state=False
    ) as harness:
        yield harness


@pytest_asyncio.fixture
async def migrated_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> AsyncGenerator[LiveBootHarness, None]:
    async with booted_harness(monkeypatch, tmp_path) as harness:
        yield harness


async def _post(harness: LiveBootHarness, path: str) -> dict:
    async def _override() -> AsyncGenerator:
        async with harness.session_factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = _override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                path, params={"tenant_id": TENANT, "greenhouse_id": GREENHOUSE}
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200, response.text
    return response.json()


class TestUnmigratedBootDoesNotCrash:

    @pytest.mark.asyncio
    async def test_api_boots_with_the_live_runtime_up(
        self, unmigrated_harness: LiveBootHarness
    ) -> None:
        """The nested try in lifespan must not null out a healthy runtime."""
        async with unmigrated_harness.boot() as runtime:
            assert runtime is not None
            assert (TENANT, GREENHOUSE) in runtime.configured_scopes()

    @pytest.mark.asyncio
    async def test_unreadable_state_leaves_every_scope_idle(
        self, unmigrated_harness: LiveBootHarness
    ) -> None:
        async with unmigrated_harness.boot() as runtime:
            assert not runtime.is_scope_active(TENANT, GREENHOUSE)
            await asyncio.sleep(IDLE_WATCH_SECONDS)
            assert await unmigrated_harness.reading_sources(RawReading) == []
            assert await unmigrated_harness.reading_sources(SensorReading) == []

    @pytest.mark.asyncio
    async def test_boot_warns_that_the_table_is_unreadable(
        self, unmigrated_harness: LiveBootHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silence here would make a skewed deploy indistinguishable from a
        deployment where nobody had pressed Go Live."""
        logger_name = live_scope_state_service.logger.name
        with caplog.at_level(logging.WARNING, logger=logger_name):
            async with unmigrated_harness.boot():
                pass

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.name == logger_name and record.levelno == logging.WARNING
        ]
        assert any("live_scope_state" in message for message in warnings), warnings
        assert any("028" in message for message in warnings), warnings


class TestUnmigratedEndpointsReportHonestly:

    @pytest.mark.asyncio
    async def test_resume_streams_but_reports_the_write_failed(
        self, unmigrated_harness: LiveBootHarness
    ) -> None:
        """Go Live still works for this process; it just will not survive."""
        async with unmigrated_harness.boot() as runtime:
            body = await _post(unmigrated_harness, "/api/live/resume")
            assert body["resumed"] is True
            assert body["scope_status"] == "started"
            assert body["desired_state_persisted"] is False
            assert runtime.is_scope_active(TENANT, GREENHOUSE)
            sources = await unmigrated_harness.await_reading_sources(RawReading)
            assert set(sources) == {"live"}

    @pytest.mark.asyncio
    async def test_pause_also_reports_the_write_failed(
        self, unmigrated_harness: LiveBootHarness
    ) -> None:
        async with unmigrated_harness.boot() as runtime:
            await _post(unmigrated_harness, "/api/live/resume")
            body = await _post(unmigrated_harness, "/api/live/pause")
            assert body["paused"] is True
            assert body["desired_state_persisted"] is False
            assert not runtime.is_scope_active(TENANT, GREENHOUSE)

    @pytest.mark.asyncio
    async def test_the_unpersisted_go_live_does_not_survive_a_restart(
        self, unmigrated_harness: LiveBootHarness
    ) -> None:
        """``desired_state_persisted: false`` is a promise, and it is kept."""
        async with unmigrated_harness.boot():
            await _post(unmigrated_harness, "/api/live/resume")
        await unmigrated_harness.clear_reading_rows()

        async with unmigrated_harness.boot() as runtime:
            assert not runtime.is_scope_active(TENANT, GREENHOUSE)
            await asyncio.sleep(IDLE_WATCH_SECONDS)
            assert await unmigrated_harness.reading_sources(RawReading) == []

    @pytest.mark.asyncio
    async def test_reading_one_scopes_state_degrades_to_none(
        self, unmigrated_harness: LiveBootHarness
    ) -> None:
        """``fetch_desired_state`` has its own missing-table guard."""
        async with unmigrated_harness.session_factory() as session:
            stored = await fetch_desired_state(
                session, tenant_id=TENANT, greenhouse_id=GREENHOUSE
            )
        assert stored is None


class TestMigratedEndpointsPersistForReal:
    """The control group: with 028 applied the same calls report True.

    Without this pair, ``desired_state_persisted is False`` above could be
    passing because the flag is hard-wired false rather than because the
    table is missing.
    """

    @pytest.mark.asyncio
    async def test_resume_persists_live(
        self, migrated_harness: LiveBootHarness
    ) -> None:
        async with migrated_harness.boot():
            body = await _post(migrated_harness, "/api/live/resume")
        assert body["desired_state_persisted"] is True

        async with migrated_harness.session_factory() as session:
            stored = await fetch_desired_state(
                session, tenant_id=TENANT, greenhouse_id=GREENHOUSE
            )
        assert stored is DesiredScopeState.LIVE

    @pytest.mark.asyncio
    async def test_pause_persists_paused(
        self, migrated_harness: LiveBootHarness
    ) -> None:
        async with migrated_harness.boot():
            await _post(migrated_harness, "/api/live/resume")
            body = await _post(migrated_harness, "/api/live/pause")
        assert body["desired_state_persisted"] is True

        async with migrated_harness.session_factory() as session:
            stored = await fetch_desired_state(
                session, tenant_id=TENANT, greenhouse_id=GREENHOUSE
            )
        assert stored is DesiredScopeState.PAUSED

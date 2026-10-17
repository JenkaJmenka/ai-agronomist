"""Acceptance: what an API restart does to a scope the operator turned on.

Each case runs ``src.api.main.lifespan`` twice against one database — the
second run *is* the redeploy.  Assertions are on rows and log lines, not on
the stored enum: "the state survived" was already true of a feature that
left tenant_2/gh_A silent for a month.

See :mod:`tests.api.live_boot_harness` for what is and is not stubbed.
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
from src.api.services import live_boot_resume
from src.api.services.live_scope_state_service import (
    DesiredScopeState,
    fetch_desired_state,
)
from src.api.services.tenant_data_wipe import wipe_tenant_data
from src.db.models import RawReading, SensorReading
from src.db.session import get_db
from src.live.scope_control import clear_all_suppressed_scopes
from tests.api.live_boot_harness import (
    GREENHOUSE,
    IDLE_WATCH_SECONDS,
    OTHER_SCOPE,
    SCOPE,
    TENANT,
    LiveBootHarness,
    booted_harness,
    set_configured_scopes,
)


@pytest.fixture(autouse=True)
def _reset_process_state() -> None:
    """No in-process live state may leak into or out of a case."""
    clear_all_suppressed_scopes()
    clear_test_api_keys()
    yield
    clear_all_suppressed_scopes()
    clear_test_api_keys()
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture
async def harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> AsyncGenerator[LiveBootHarness, None]:
    async with booted_harness(monkeypatch, tmp_path) as live_harness:
        yield live_harness


def _boot_resume_log(caplog: pytest.LogCaptureFixture, level: int) -> str:
    """The latest boot-resume record at *level*, as its formatted message.

    Matching against ``caplog.text`` is not safe here: ``scope_control``
    emits "Live scope resumed: tenant/greenhouse" on every Go Live, which
    contains the exact substring the resumed-bucket assertion looks for. A
    mutation run with boot resume stubbed out passed on that collision, so
    these assertions read the boot-resume logger's own records instead.
    """
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == live_boot_resume.logger.name and record.levelno == level
    ]
    assert messages, f"no boot-resume record at level {level}"
    return messages[-1]


async def _press_go_live(harness: LiveBootHarness, path: str) -> dict:
    """Drive POST /api/live/{resume,pause} the way the Go Live toggle does."""

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


class TestRestartResumesGoLive:
    """Item 1: Go Live -> restart -> running again *and writing rows*."""

    @pytest.mark.asyncio
    async def test_resumed_scope_streams_rows_after_a_restart(
        self, harness: LiveBootHarness
    ) -> None:
        async with harness.boot() as runtime:
            assert runtime is not None
            assert not runtime.is_scope_active(TENANT, GREENHOUSE)
            body = await _press_go_live(harness, "/api/live/resume")
            assert body["resumed"] is True
            assert body["desired_state_persisted"] is True

        # Rows written before the restart would make a post-restart row count
        # meaningless, so the slate is wiped between processes.
        await harness.clear_reading_rows()

        async with harness.boot() as runtime:
            assert runtime.is_scope_active(TENANT, GREENHOUSE)
            raw_sources = await harness.await_reading_sources(RawReading)
            assert raw_sources, "resumed scope wrote no raw_readings row after restart"
            assert set(raw_sources) == {"live"}

    @pytest.mark.asyncio
    async def test_restart_also_reaches_the_canonical_table(
        self, harness: LiveBootHarness
    ) -> None:
        """A complete bucket must land in sensor_readings too, not just raw."""
        async with harness.boot():
            await _press_go_live(harness, "/api/live/resume")
        await harness.clear_reading_rows()

        async with harness.boot():
            sensor_sources = await harness.await_reading_sources(SensorReading)
            assert sensor_sources, "no sensor_readings row after restart"
            assert set(sensor_sources) == {"live"}

    @pytest.mark.asyncio
    async def test_boot_log_names_the_resumed_scope(
        self, harness: LiveBootHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with harness.boot():
            await _press_go_live(harness, "/api/live/resume")

        caplog.clear()
        with caplog.at_level(logging.INFO, logger=live_boot_resume.logger.name):
            async with harness.boot():
                pass

        startup = _boot_resume_log(caplog, logging.INFO)
        assert f"resumed: {TENANT}/{GREENHOUSE}" in startup


class TestRestartKeepsAPausedScopeIdle:
    """Item 2: Pause -> restart -> idle, and nothing is written."""

    @pytest.mark.asyncio
    async def test_paused_scope_stays_idle_and_writes_nothing(
        self, harness: LiveBootHarness
    ) -> None:
        async with harness.boot():
            await _press_go_live(harness, "/api/live/resume")
            pause_body = await _press_go_live(harness, "/api/live/pause")
            assert pause_body["paused"] is True
            assert pause_body["desired_state_persisted"] is True
        await harness.clear_reading_rows()

        async with harness.boot() as runtime:
            assert not runtime.is_scope_active(TENANT, GREENHOUSE)
            await asyncio.sleep(IDLE_WATCH_SECONDS)
            assert await harness.reading_sources(RawReading) == []
            assert await harness.reading_sources(SensorReading) == []

    @pytest.mark.asyncio
    async def test_paused_scope_is_reported_idle_not_resumed(
        self, harness: LiveBootHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with harness.boot():
            await _press_go_live(harness, "/api/live/resume")
            await _press_go_live(harness, "/api/live/pause")

        caplog.clear()
        with caplog.at_level(logging.INFO, logger=live_boot_resume.logger.name):
            async with harness.boot():
                pass

        startup = _boot_resume_log(caplog, logging.INFO)
        assert "resumed: (none)" in startup
        assert f"idle: {TENANT}/{GREENHOUSE}" in startup


class TestWipeSurvivesTheRestart:
    """Item 3: Clear all data -> restart -> still idle."""

    @pytest.mark.asyncio
    async def test_wiped_scope_stays_idle_after_a_restart(
        self, harness: LiveBootHarness
    ) -> None:
        async with harness.boot():
            await _press_go_live(harness, "/api/live/resume")

        async with harness.session_factory() as session:
            await wipe_tenant_data(session, None, TENANT, GREENHOUSE)
            assert (
                await fetch_desired_state(
                    session, tenant_id=TENANT, greenhouse_id=GREENHOUSE
                )
                is DesiredScopeState.PAUSED
            )

        # The wipe's in-process suppression flag dies with the process it was
        # set in; clearing it here keeps the next assertion about the
        # *persisted* state rather than about a leftover gate.
        clear_all_suppressed_scopes()

        async with harness.boot() as runtime:
            assert not runtime.is_scope_active(TENANT, GREENHOUSE)
            await asyncio.sleep(IDLE_WATCH_SECONDS)
            assert await harness.reading_sources(RawReading) == []
            assert await harness.reading_sources(SensorReading) == []


class TestScopeDroppedFromEnv:
    """Item 4: scope removed from MOCK_SCADA_SCOPES -> idle, and named."""

    @pytest.mark.asyncio
    async def test_dropped_scope_is_named_in_the_boot_log_and_stays_dark(
        self,
        harness: LiveBootHarness,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async with harness.boot() as runtime:
            assert (TENANT, GREENHOUSE) in runtime.configured_scopes()
            await _press_go_live(harness, "/api/live/resume")
        await harness.clear_reading_rows()

        # The operator edits the env var and redeploys.
        set_configured_scopes(monkeypatch, [OTHER_SCOPE])

        caplog.clear()
        with caplog.at_level(logging.INFO, logger=live_boot_resume.logger.name):
            async with harness.boot() as runtime:
                assert (TENANT, GREENHOUSE) not in runtime.configured_scopes()
                assert not runtime.is_scope_active(TENANT, GREENHOUSE)
                await asyncio.sleep(IDLE_WATCH_SECONDS)
                assert await harness.reading_sources(RawReading) == []

        startup = _boot_resume_log(caplog, logging.INFO)
        assert f"stored live but not configured: {TENANT}/{GREENHOUSE}" in startup
        assert "resumed: (none)" in startup

    @pytest.mark.asyncio
    async def test_dropped_scope_also_raises_a_warning_naming_it(
        self,
        harness: LiveBootHarness,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A dropped feed must be diagnosable at WARNING, not buried in INFO."""
        async with harness.boot():
            await _press_go_live(harness, "/api/live/resume")

        set_configured_scopes(monkeypatch, [OTHER_SCOPE])
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=live_boot_resume.logger.name):
            async with harness.boot():
                pass

        warning = _boot_resume_log(caplog, logging.WARNING)
        assert f"{TENANT}/{GREENHOUSE}" in warning
        assert "MOCK_SCADA_SCOPES" in warning

    @pytest.mark.asyncio
    async def test_restoring_the_env_entry_resumes_the_scope(
        self, harness: LiveBootHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stored row is not discarded while the scope is unconfigured."""
        async with harness.boot():
            await _press_go_live(harness, "/api/live/resume")

        set_configured_scopes(monkeypatch, [OTHER_SCOPE])
        async with harness.boot() as runtime:
            assert not runtime.is_scope_active(TENANT, GREENHOUSE)

        set_configured_scopes(monkeypatch, [SCOPE])
        await harness.clear_reading_rows()
        async with harness.boot() as runtime:
            assert runtime.is_scope_active(TENANT, GREENHOUSE)
            assert set(await harness.await_reading_sources(RawReading)) == {"live"}

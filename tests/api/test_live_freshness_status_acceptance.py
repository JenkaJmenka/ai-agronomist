"""Acceptance: ``GET /api/live/status`` freshness, over a real live scope.

"Go Live is on" and "the feed works" are different claims — a production
audit found zero ``source='live'`` rows while the UI reported a configured
scope.  Backend covered :mod:`src.api.services.live_freshness` directly and
covered the endpoint once, for a ``not_configured`` MagicMock.  These cases
cover the shape the dashboard actually receives: a genuinely booted runtime
whose scope is running, with rows at controlled ages.
"""

from __future__ import annotations

import datetime
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.auth import clear_test_api_keys
from src.api.main import app
from src.api.services import live_freshness
from src.config import LIVE_STALE_AFTER_SECONDS
from src.db.models import RawReading, SensorReading
from src.db.session import get_db
from src.live.scope_control import clear_all_suppressed_scopes
from tests.api.live_boot_harness import (
    GREENHOUSE,
    TENANT,
    LiveBootHarness,
    booted_harness,
)

# Comfortably past / inside the threshold, so a few seconds of wall-clock
# drift during the test cannot flip either verdict.
_STALE_MARGIN_SECONDS = 600.0
_FRESH_AGE_SECONDS = 1.0


@pytest.fixture(autouse=True)
def _reset_process_state() -> None:
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


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def _seed_sensor_reading(
    harness: LiveBootHarness, timestamp: datetime.datetime
) -> None:
    async with harness.session_factory() as session:
        session.add(
            SensorReading(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                timestamp=timestamp,
                temperature=22.0,
                humidity=60.0,
                co2=400.0,
                light=500.0,
                source="live",
            )
        )
        await session.commit()


async def _get_status(harness: LiveBootHarness) -> dict[str, Any]:
    async def _override() -> AsyncGenerator:
        async with harness.session_factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = _override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/live/status",
                params={"tenant_id": TENANT, "greenhouse_id": GREENHOUSE},
            )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 200, response.text
    return response.json()


class TestStaleLiveScope:
    """Item 5: an old newest-row makes a live scope stale; a fresh one clears it."""

    @pytest.mark.asyncio
    async def test_live_scope_past_the_threshold_reports_stale(
        self, harness: LiveBootHarness
    ) -> None:
        async with harness.boot() as runtime:
            await runtime.start_scope(TENANT, GREENHOUSE)
            await harness.clear_reading_rows()
            await _seed_sensor_reading(
                harness,
                _utc_now()
                - datetime.timedelta(
                    seconds=LIVE_STALE_AFTER_SECONDS + _STALE_MARGIN_SECONDS
                ),
            )

            freshness = (await _get_status(harness))["freshness"]

        assert freshness["state"] == "live"
        assert freshness["is_stale"] is True
        assert freshness["age_seconds"] > LIVE_STALE_AFTER_SECONDS

    @pytest.mark.asyncio
    async def test_a_fresh_row_clears_the_stale_flag(
        self, harness: LiveBootHarness
    ) -> None:
        async with harness.boot() as runtime:
            await runtime.start_scope(TENANT, GREENHOUSE)
            await harness.clear_reading_rows()
            await _seed_sensor_reading(
                harness,
                _utc_now()
                - datetime.timedelta(
                    seconds=LIVE_STALE_AFTER_SECONDS + _STALE_MARGIN_SECONDS
                ),
            )
            assert (await _get_status(harness))["freshness"]["is_stale"] is True

            await _seed_sensor_reading(
                harness,
                _utc_now() - datetime.timedelta(seconds=_FRESH_AGE_SECONDS),
            )
            cleared = (await _get_status(harness))["freshness"]

        assert cleared["is_stale"] is False
        assert cleared["age_seconds"] < LIVE_STALE_AFTER_SECONDS

    @pytest.mark.asyncio
    async def test_rows_written_by_the_connector_clear_the_stale_flag(
        self, harness: LiveBootHarness
    ) -> None:
        """End to end: a live scope that has never written is stale, and the
        feed itself — not a seeded row — is what clears it."""
        async with harness.boot() as runtime:
            await harness.clear_reading_rows()
            before = (await _get_status(harness))["freshness"]
            assert before["state"] == "paused"

            await runtime.start_scope(TENANT, GREENHOUSE)
            stale = (await _get_status(harness))["freshness"]
            assert stale["state"] == "live"
            assert stale["newest_reading_at"] is None
            assert stale["is_stale"] is True, "a live scope with no rows is stale"

            assert set(await harness.await_reading_sources(RawReading)) == {"live"}
            after = (await _get_status(harness))["freshness"]

        assert after["newest_reading_at"] is not None
        assert after["is_stale"] is False

    @pytest.mark.asyncio
    async def test_threshold_in_the_response_comes_from_config(
        self, harness: LiveBootHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The threshold is a config key, not a literal baked into the route.

        Asserted by moving it: a row that is fresh at the shipped 180 s must
        read stale once the threshold drops below its age.
        """
        age_seconds = LIVE_STALE_AFTER_SECONDS / 2
        async with harness.boot() as runtime:
            await runtime.start_scope(TENANT, GREENHOUSE)
            await harness.clear_reading_rows()
            await _seed_sensor_reading(
                harness, _utc_now() - datetime.timedelta(seconds=age_seconds)
            )

            shipped = (await _get_status(harness))["freshness"]
            assert shipped["threshold_seconds"] == LIVE_STALE_AFTER_SECONDS
            assert shipped["is_stale"] is False

            monkeypatch.setattr(
                live_freshness, "LIVE_STALE_AFTER_SECONDS", age_seconds / 2
            )
            tightened = (await _get_status(harness))["freshness"]

        assert tightened["threshold_seconds"] == age_seconds / 2
        assert tightened["is_stale"] is True


class TestNonLiveScopesAreNeverStale:

    @pytest.mark.asyncio
    async def test_idle_scope_with_ancient_rows_is_paused_not_stale(
        self, harness: LiveBootHarness
    ) -> None:
        """Most scopes are idle most of the time; flagging them is noise."""
        async with harness.boot():
            await harness.clear_reading_rows()
            await _seed_sensor_reading(
                harness, _utc_now() - datetime.timedelta(days=365)
            )
            freshness = (await _get_status(harness))["freshness"]

        assert freshness["state"] == "paused"
        assert freshness["is_stale"] is False
        assert freshness["age_seconds"] > LIVE_STALE_AFTER_SECONDS


class TestFutureDatedRowsThroughTheEndpoint:
    """Readings in this repo run ahead of wall clock by design."""

    @pytest.mark.asyncio
    async def test_a_future_dated_row_gives_a_negative_age_and_is_not_stale(
        self, harness: LiveBootHarness
    ) -> None:
        async with harness.boot() as runtime:
            await runtime.start_scope(TENANT, GREENHOUSE)
            await harness.clear_reading_rows()
            await _seed_sensor_reading(
                harness, _utc_now() + datetime.timedelta(days=60)
            )
            freshness = (await _get_status(harness))["freshness"]

        assert freshness["state"] == "live"
        assert freshness["age_seconds"] < 0, "age must not be clamped to zero"
        assert freshness["is_stale"] is False


class TestStatusContractIsUnchanged:
    """``sources`` and ``greenhouse_status`` must still answer as before."""

    @pytest.mark.asyncio
    async def test_a_running_scope_reports_live_with_its_sources(
        self, harness: LiveBootHarness
    ) -> None:
        async with harness.boot() as runtime:
            await runtime.start_scope(TENANT, GREENHOUSE)
            body = await _get_status(harness)

        assert body["greenhouse_status"] == "live"
        assert body["sources"], "a configured scope must list its sources"
        assert body["freshness"]["state"] == "live"

    @pytest.mark.asyncio
    async def test_freshness_counts_a_raw_only_bucket(
        self, harness: LiveBootHarness
    ) -> None:
        """An incomplete bucket reaches raw_readings only — still a live feed."""
        async with harness.boot() as runtime:
            await runtime.start_scope(TENANT, GREENHOUSE)
            await harness.clear_reading_rows()
            async with harness.session_factory() as session:
                session.add(
                    RawReading(
                        tenant_id=TENANT,
                        greenhouse_id=GREENHOUSE,
                        timestamp=_utc_now()
                        - datetime.timedelta(seconds=_FRESH_AGE_SECONDS),
                        source="live",
                        data={"temperature": 22.0},
                    )
                )
                await session.commit()
            freshness = (await _get_status(harness))["freshness"]

        assert freshness["newest_reading_at"] is not None
        assert freshness["is_stale"] is False

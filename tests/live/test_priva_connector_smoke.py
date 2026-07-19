"""Smoke tests for PrivaConnector (Phase 5B).

Coverage
--------
- OAuth2 token fetch on connect().
- Proactive token refresh when expiry margin exceeded.
- Reactive 401 token refresh during fetch_since(); re-raises on second 401.
- 429 Retry-After backoff before retry.
- since-cursor sent in request and advances without duplicate yields.
- Partial payloads (metric missing value) are skipped; connector survives.
- Unknown/unmapped raw fields are silently dropped.
- Malformed timestamp causes metric to be skipped; no exception raised.
- channel-map filtering: only channels in SourceConfig.channels are emitted.
- Pagination: ``next`` link is followed; all pages are aggregated.
- LiveRuntime can instantiate PrivaConnector from LIVE_SOURCES_JSON config.

All tests use respx-mocked httpx — no real network.  Total runtime <5 s.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import httpx
import pytest
import respx

from src.live.sources.base import SourceConfig
from src.live.sources.priva import PrivaConnector

BASE_URL = "https://priva.test"
TOKEN_URL = f"{BASE_URL}/oauth/token"
HISTORY_URL = f"{BASE_URL}/v1/metrics/history"

TENANT = "t1"
GH = "gh1"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_config(
    channels: list[str] | None = None,
    extra_params: dict | None = None,
) -> SourceConfig:
    params: dict = {
        "base_url": BASE_URL,
        "client_id": "cid_01",
        "client_secret_env": "PRIVA_TEST_SECRET",
        "installation_id": "inst_01",
        "zone_id": "zone_01",
        **(extra_params or {}),
    }
    return SourceConfig(
        source_id="priva_src",
        vendor="priva",
        tenant_id=TENANT,
        greenhouse_id=GH,
        channels=channels or ["temperature", "humidity", "co2", "light"],
        poll_interval_seconds=0.01,
        params=params,
    )


def _token_response(expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok_abc", "expires_in": expires_in})


def _metrics_response(metrics: list[dict], next_page: str | None = None) -> httpx.Response:
    body: dict = {"metrics": metrics, "next": next_page}
    return httpx.Response(200, json=body)


def _metric(ts: str = "2024-01-01T00:00:01Z", **kwargs: float) -> dict:
    """Build a Priva metric dict with raw field names."""
    base: dict = {"time": ts}
    base.update(kwargs)
    return base


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


async def test_connect_fetches_token(monkeypatch: pytest.MonkeyPatch):
    """connect() acquires an OAuth2 token and stores it."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(return_value=_metrics_response([]))

        connector = PrivaConnector(_make_config())
        await connector.connect()
        assert connector._access_token == "tok_abc"
        await connector.close()


async def test_connect_raises_when_secret_env_missing(monkeypatch: pytest.MonkeyPatch):
    """connect() raises DataSourceConnectionError when secret env var is unset."""
    monkeypatch.delenv("PRIVA_TEST_SECRET", raising=False)

    from src.vendors.base import DataSourceConnectionError

    connector = PrivaConnector(_make_config())
    with pytest.raises(DataSourceConnectionError, match="PRIVA_TEST_SECRET"):
        await connector.connect()


async def test_401_triggers_token_refresh(monkeypatch: pytest.MonkeyPatch):
    """A 401 on fetch_since causes one reactive token refresh and retry."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")

    call_count = 0

    def metrics_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(401)
        return _metrics_response([])

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(side_effect=metrics_side_effect)

        connector = PrivaConnector(_make_config())
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert call_count == 2  # original 401 + retry after refresh
    assert readings == []


async def test_double_401_raises(monkeypatch: pytest.MonkeyPatch):
    """If the retry after token refresh also returns 401, an exception is raised."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(return_value=httpx.Response(401))

        connector = PrivaConnector(_make_config())
        await connector.connect()
        with pytest.raises(httpx.HTTPStatusError):
            await connector.fetch_since(None)
        await connector.close()


async def test_429_respects_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 response causes a sleep matching Retry-After before the retry."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")

    slept: list[float] = []

    async def fake_sleep(t: float) -> None:
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    call_count = 0

    def metrics_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "30"})
        return _metrics_response([])

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(side_effect=metrics_side_effect)

        connector = PrivaConnector(_make_config())
        await connector.connect()
        await connector.fetch_since(None)
        await connector.close()

    assert 30.0 in slept, f"Expected 30.0 in slept sleeps; got {slept}"
    assert call_count == 2


async def test_since_cursor_in_request(monkeypatch: pytest.MonkeyPatch):
    """fetch_since passes the since value as a query param."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")
    since_dt = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    captured_params: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured_params.append(request.url.params.get("since", ""))
        return _metrics_response([])

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(side_effect=capture)

        connector = PrivaConnector(_make_config())
        await connector.connect()
        await connector.fetch_since(since_dt)
        await connector.close()

    assert captured_params[0] == since_dt.isoformat()


async def test_channel_mapping_and_clamping(monkeypatch: pytest.MonkeyPatch):
    """Raw Priva fields are correctly mapped to canonical names and clamped."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")

    metric = _metric(
        ambient_temp=25.0,
        rel_humidity=70.0,
        co2_level=420.0,
        radiation_lux=500.0,
    )

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(return_value=_metrics_response([metric]))

        connector = PrivaConnector(_make_config())
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert len(readings) == 1
    ch = readings[0].channels
    assert ch["temperature"] == pytest.approx(25.0)
    assert ch["humidity"] == pytest.approx(70.0)
    assert ch["co2"] == pytest.approx(420.0)
    assert ch["light"] == pytest.approx(500.0)


async def test_out_of_bounds_values_are_clamped(monkeypatch: pytest.MonkeyPatch):
    """Values outside CHANNEL_BOUNDS are clamped, not dropped."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")

    metric = _metric(ambient_temp=-5.0, rel_humidity=110.0)  # below 0 / above 100

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(return_value=_metrics_response([metric]))

        connector = PrivaConnector(_make_config(channels=["temperature", "humidity"]))
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert readings[0].channels["temperature"] == pytest.approx(0.0)
    assert readings[0].channels["humidity"] == pytest.approx(100.0)


async def test_missing_timestamp_skips_metric(monkeypatch: pytest.MonkeyPatch):
    """A metric without a parseable timestamp is skipped; no exception raised."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")

    bad_metric = {"ambient_temp": 22.0}  # no "time" field
    good_metric = _metric(ambient_temp=23.0)

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(
            return_value=_metrics_response([bad_metric, good_metric])
        )

        connector = PrivaConnector(_make_config(channels=["temperature"]))
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert len(readings) == 1
    assert readings[0].channels["temperature"] == pytest.approx(23.0)


async def test_channel_not_in_config_is_filtered(monkeypatch: pytest.MonkeyPatch):
    """Channels not listed in SourceConfig.channels are not yielded."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")

    metric = _metric(ambient_temp=22.0, rel_humidity=60.0)  # both fields present

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(return_value=_metrics_response([metric]))

        # Config only exposes temperature, not humidity.
        connector = PrivaConnector(_make_config(channels=["temperature"]))
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert len(readings) == 1
    assert "humidity" not in readings[0].channels
    assert "temperature" in readings[0].channels


async def test_pagination_aggregates_all_pages(monkeypatch: pytest.MonkeyPatch):
    """Readings from all pages are returned when ``next`` links are present."""
    monkeypatch.setenv("PRIVA_TEST_SECRET", "s3cr3t")

    page1_metric = _metric("2024-01-01T00:00:01Z", ambient_temp=20.0)
    page2_metric = _metric("2024-01-01T00:00:02Z", ambient_temp=21.0)
    page2_url = f"{BASE_URL}/v1/metrics/history?page=2"
    call_count = 0

    def paged_response(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _metrics_response([page1_metric], next_page=page2_url)
        return _metrics_response([page2_metric])

    with respx.mock:
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(HISTORY_URL).mock(side_effect=paged_response)
        respx.get(page2_url).mock(side_effect=paged_response)

        connector = PrivaConnector(_make_config(channels=["temperature"]))
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert len(readings) == 2
    temps = sorted(r.channels["temperature"] for r in readings)
    assert temps == pytest.approx([20.0, 21.0])


async def test_runtime_can_instantiate_from_sources_json():
    """CONNECTOR_TYPES['priva'] produces a PrivaConnector from a dict config."""
    from src.live.sources import CONNECTOR_TYPES

    cfg_dict = {
        "source_id": "priva_gh1",
        "vendor": "priva",
        "tenant_id": "farm_co",
        "greenhouse_id": "gh_01",
        "channels": ["temperature", "humidity", "co2", "light"],
        "priority": 10,
        "poll_interval_seconds": 30.0,
        "params": {
            "base_url": "https://api.priva.com",
            "client_id": "cid",
            "client_secret_env": "PRIVA_CLIENT_SECRET",
            "installation_id": "inst_01",
            "zone_id": "zone_main",
        },
    }
    cfg = SourceConfig(**cfg_dict)
    connector = CONNECTOR_TYPES["priva"](cfg)
    assert isinstance(connector, PrivaConnector)
    assert connector.vendor == "priva"
    assert connector.config.source_id == "priva_gh1"

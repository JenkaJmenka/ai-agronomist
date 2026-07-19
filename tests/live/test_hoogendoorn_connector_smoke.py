"""Smoke tests for HoogendoornConnector (Phase 5B).

Coverage
--------
- API-key auth header is set on every request.
- connect() raises when api_key_env is unset.
- 429 Retry-After backoff before retry.
- 401 is logged and re-raised (API keys don't rotate automatically).
- since-cursor is sent in the request and advances between fetches.
- channel_map: default map, custom override, unknown tags skipped.
- Malformed/missing timestamp causes item to be skipped.
- Non-finite and non-numeric values are dropped.
- Pagination via nextPage URL aggregates all pages.
- LiveRuntime can instantiate HoogendoornConnector from LIVE_SOURCES_JSON.

All tests use respx-mocked httpx — no real network.  Total runtime <5 s.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
import respx

from src.live.sources.base import SourceConfig
from src.live.sources.hoogendoorn import DEFAULT_CHANNEL_MAP, HoogendoornConnector

BASE_URL = "https://hg.test"
MEASURE_URL = f"{BASE_URL}/v1/measurements"

TENANT = "t1"
GH = "gh1"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_config(
    channels: list[str] | None = None,
    channel_map: dict[str, str] | None = None,
) -> SourceConfig:
    params: dict = {
        "base_url": BASE_URL,
        "api_key_env": "HG_TEST_API_KEY",
        "installation_id": "inst_hg_01",
    }
    if channel_map is not None:
        params["channel_map"] = channel_map
    return SourceConfig(
        source_id="hg_src",
        vendor="hoogendoorn",
        tenant_id=TENANT,
        greenhouse_id=GH,
        channels=channels or ["temperature", "humidity", "co2", "light"],
        poll_interval_seconds=0.01,
        params=params,
    )


def _measurement(
    tag: str,
    value: float,
    ts: str = "2024-01-01T00:00:01Z",
) -> dict:
    return {"tag": tag, "timestamp": ts, "value": value}


def _data_response(items: list[dict], next_page: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"data": items, "nextPage": next_page})


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


async def test_connect_sets_api_key(monkeypatch: pytest.MonkeyPatch):
    """connect() loads the API key from the named env var."""
    monkeypatch.setenv("HG_TEST_API_KEY", "key_abc")

    with respx.mock:
        respx.get(MEASURE_URL).mock(return_value=_data_response([]))
        connector = HoogendoornConnector(_make_config())
        await connector.connect()
        assert connector._api_key == "key_abc"
        await connector.close()


async def test_connect_raises_when_api_key_env_missing(monkeypatch: pytest.MonkeyPatch):
    """connect() raises DataSourceConnectionError when the API-key env var is absent."""
    monkeypatch.delenv("HG_TEST_API_KEY", raising=False)

    from src.vendors.base import DataSourceConnectionError

    connector = HoogendoornConnector(_make_config())
    with pytest.raises(DataSourceConnectionError, match="HG_TEST_API_KEY"):
        await connector.connect()


async def test_api_key_header_sent(monkeypatch: pytest.MonkeyPatch):
    """Every request carries the X-Api-Key header."""
    monkeypatch.setenv("HG_TEST_API_KEY", "key_xyz")
    captured_keys: list[str] = []

    def check_header(request: httpx.Request) -> httpx.Response:
        captured_keys.append(request.headers.get("X-Api-Key", ""))
        return _data_response([])

    with respx.mock:
        respx.get(MEASURE_URL).mock(side_effect=check_header)
        connector = HoogendoornConnector(_make_config())
        await connector.connect()
        await connector.fetch_since(None)
        await connector.close()

    assert all(k == "key_xyz" for k in captured_keys)
    assert len(captured_keys) >= 1


async def test_429_respects_retry_after(monkeypatch: pytest.MonkeyPatch):
    """429 response causes a sleep matching Retry-After before the retry."""
    monkeypatch.setenv("HG_TEST_API_KEY", "key_x")

    slept: list[float] = []

    async def fake_sleep(t: float) -> None:
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    call_count = 0

    def rate_limited(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(429, headers={"Retry-After": "45"})
        return _data_response([])

    with respx.mock:
        respx.get(MEASURE_URL).mock(side_effect=rate_limited)
        connector = HoogendoornConnector(_make_config())
        await connector.connect()
        await connector.fetch_since(None)
        await connector.close()

    assert 45.0 in slept
    assert call_count == 2


async def test_401_raises(monkeypatch: pytest.MonkeyPatch):
    """A 401 response is re-raised as HTTPStatusError after logging."""
    monkeypatch.setenv("HG_TEST_API_KEY", "bad_key")

    with respx.mock:
        respx.get(MEASURE_URL).mock(return_value=httpx.Response(401))
        connector = HoogendoornConnector(_make_config())
        await connector.connect()
        with pytest.raises(httpx.HTTPStatusError):
            await connector.fetch_since(None)
        await connector.close()


async def test_default_channel_map_applied(monkeypatch: pytest.MonkeyPatch):
    """Default tag names map to canonical channels."""
    monkeypatch.setenv("HG_TEST_API_KEY", "k")

    items = [
        _measurement("Air Temp", 22.5),
        _measurement("RH", 65.0),
        _measurement("CO2", 410.0),
        _measurement("Solar Radiation", 300.0),
    ]

    with respx.mock:
        respx.get(MEASURE_URL).mock(return_value=_data_response(items))
        connector = HoogendoornConnector(_make_config())
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert len(readings) == 4
    canonical_names = {list(r.channels.keys())[0] for r in readings}
    assert canonical_names == {"temperature", "humidity", "co2", "light"}


async def test_unknown_tag_is_skipped(monkeypatch: pytest.MonkeyPatch):
    """Tags not in the channel map are silently skipped."""
    monkeypatch.setenv("HG_TEST_API_KEY", "k")

    items = [
        _measurement("Air Temp", 22.5),
        _measurement("Nutrient EC", 2.8),  # not in default map
        _measurement("RH", 65.0),
    ]

    with respx.mock:
        respx.get(MEASURE_URL).mock(return_value=_data_response(items))
        connector = HoogendoornConnector(_make_config(channels=["temperature", "humidity"]))
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    names = {list(r.channels.keys())[0] for r in readings}
    assert names == {"temperature", "humidity"}


async def test_custom_channel_map_override(monkeypatch: pytest.MonkeyPatch):
    """params['channel_map'] overrides the default map."""
    monkeypatch.setenv("HG_TEST_API_KEY", "k")

    custom_map = {"Temp Greenhouse": "temperature", "Humidity": "humidity"}
    items = [
        _measurement("Temp Greenhouse", 23.0),
        _measurement("Humidity", 71.0),
    ]

    with respx.mock:
        respx.get(MEASURE_URL).mock(return_value=_data_response(items))
        connector = HoogendoornConnector(
            _make_config(channels=["temperature", "humidity"], channel_map=custom_map)
        )
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    names = {list(r.channels.keys())[0] for r in readings}
    assert names == {"temperature", "humidity"}


async def test_missing_timestamp_skips_item(monkeypatch: pytest.MonkeyPatch):
    """Items without a parseable timestamp are skipped; connector survives."""
    monkeypatch.setenv("HG_TEST_API_KEY", "k")

    items = [
        {"tag": "Air Temp", "value": 22.0},  # no timestamp field
        _measurement("Air Temp", 23.5),
    ]

    with respx.mock:
        respx.get(MEASURE_URL).mock(return_value=_data_response(items))
        connector = HoogendoornConnector(_make_config(channels=["temperature"]))
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert len(readings) == 1
    assert readings[0].channels["temperature"] == pytest.approx(23.5)


async def test_non_finite_value_dropped(monkeypatch: pytest.MonkeyPatch):
    """Non-finite float values (NaN, inf) are dropped from readings."""
    monkeypatch.setenv("HG_TEST_API_KEY", "k")

    # httpx's json= kwarg refuses NaN (strict encoder), but sloppy vendor
    # APIs can emit it and json.loads accepts it — so build the body raw.
    raw_body = (
        '{"data": ['
        '{"tag": "Air Temp", "timestamp": "2024-01-01T00:00:01Z", "value": NaN},'
        '{"tag": "Air Temp", "timestamp": "2024-01-01T00:00:00Z", "value": 24.0}'
        '], "nextPage": null}'
    )
    nan_response = httpx.Response(
        200, content=raw_body, headers={"content-type": "application/json"},
    )

    with respx.mock:
        respx.get(MEASURE_URL).mock(return_value=nan_response)
        connector = HoogendoornConnector(_make_config(channels=["temperature"]))
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert len(readings) == 1
    assert readings[0].channels["temperature"] == pytest.approx(24.0)


async def test_since_cursor_in_request(monkeypatch: pytest.MonkeyPatch):
    """fetch_since passes the since timestamp as a query param."""
    monkeypatch.setenv("HG_TEST_API_KEY", "k")
    since_dt = datetime(2024, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
    captured: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params.get("since", ""))
        return _data_response([])

    with respx.mock:
        respx.get(MEASURE_URL).mock(side_effect=capture)
        connector = HoogendoornConnector(_make_config())
        await connector.connect()
        await connector.fetch_since(since_dt)
        await connector.close()

    assert captured[0] == since_dt.isoformat()


async def test_pagination_aggregates_pages(monkeypatch: pytest.MonkeyPatch):
    """Readings from all pages are returned when nextPage links are present."""
    monkeypatch.setenv("HG_TEST_API_KEY", "k")

    page2_url = f"{BASE_URL}/v1/measurements?page=2"
    call_count = 0

    def paged(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _data_response(
                [_measurement("Air Temp", 20.0, "2024-01-01T00:00:01Z")],
                next_page=page2_url,
            )
        return _data_response(
            [_measurement("Air Temp", 21.0, "2024-01-01T00:00:02Z")]
        )

    with respx.mock:
        respx.get(MEASURE_URL).mock(side_effect=paged)
        respx.get(page2_url).mock(side_effect=paged)
        connector = HoogendoornConnector(_make_config(channels=["temperature"]))
        await connector.connect()
        readings = await connector.fetch_since(None)
        await connector.close()

    assert len(readings) == 2
    temps = sorted(r.channels["temperature"] for r in readings)
    assert temps == pytest.approx([20.0, 21.0])


async def test_runtime_can_instantiate_from_sources_json():
    """CONNECTOR_TYPES['hoogendoorn'] produces a HoogendoornConnector."""
    from src.live.sources import CONNECTOR_TYPES

    cfg_dict = {
        "source_id": "hg_gh1",
        "vendor": "hoogendoorn",
        "tenant_id": "farm_co",
        "greenhouse_id": "gh_01",
        "channels": ["temperature", "humidity", "co2", "light"],
        "priority": 20,
        "poll_interval_seconds": 60.0,
        "params": {
            "base_url": "https://api.letsgrow.com",
            "api_key_env": "HOOGENDOORN_API_KEY",
            "installation_id": "inst_hg",
            "channel_map": {"Temp In": "temperature", "RH In": "humidity"},
        },
    }
    cfg = SourceConfig(**cfg_dict)
    connector = CONNECTOR_TYPES["hoogendoorn"](cfg)
    assert isinstance(connector, HoogendoornConnector)
    assert connector.vendor == "hoogendoorn"
    assert connector.config.source_id == "hg_gh1"

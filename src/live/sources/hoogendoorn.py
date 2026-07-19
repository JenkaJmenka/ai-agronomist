"""Hoogendoorn (LetsGrow) SCADA connector for live greenhouse monitoring.

Connects to the LetsGrow.com REST API using an API-key header, polls
the measurements endpoint with a since-cursor, and emits
:class:`~src.live.events.SourceReading` objects for canonical channels.

Configuration (``SourceConfig.params``)
-----------------------------------------
``base_url``
    LetsGrow API root, e.g. ``https://api.letsgrow.com``.
``api_key_env``
    **Name** of the environment variable holding the API key.
    The key is never stored in config literals.
``installation_id``
    LetsGrow installation identifier.
``channel_map``  *(optional)*
    Mapping of vendor tag names to canonical channel names.
    Merged on top of :data:`DEFAULT_CHANNEL_MAP` at connect time.

    Example site-specific override::

        "channel_map": {
            "Temp In": "temperature",
            "Hum In": "humidity",
            "CO2 Meas": "co2",
            "Rad In": "light"
        }

Hoogendoorn on-prem OPC-UA (future)
--------------------------------------
Hoogendoorn hardware also exposes an OPC-UA server.  An on-prem variant
would map natively to the push-mode ``stream()`` interface — subscribe to
OPC-UA node-change events, yield on notification — rather than polling.
This connector targets the cloud / LetsGrow-hosted REST path only.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime
from typing import Any, ClassVar

import httpx

from src.live.events import CHANNEL_BOUNDS, SourceReading
from src.live.sources.base import SourceConfig
from src.live.sources.polling import PollingSourceConnector, parse_iso_timestamp
from src.vendors.base import DataSourceConnectionError

logger = logging.getLogger("live.sources.hoogendoorn")

# Default Hoogendoorn/LetsGrow tag → canonical channel mapping.
# Individual installations may use different tag names; override via
# SourceConfig.params["channel_map"] to add or replace entries.
DEFAULT_CHANNEL_MAP: dict[str, str] = {
    "Air Temp": "temperature",
    "RH": "humidity",
    "CO2": "co2",
    "Solar Radiation": "light",
}

_MEASUREMENTS_PATH: str = "/v1/measurements"
_API_KEY_HEADER: str = "X-Api-Key"

# Default Retry-After delay when the header is absent from a 429 response.
_DEFAULT_RETRY_AFTER: float = 60.0


class HoogendoornConnector(PollingSourceConnector):
    """Polls the LetsGrow REST API for Hoogendoorn greenhouse sensor readings.

    Authentication uses a static API key passed in the ``X-Api-Key`` header.
    Supports paginated responses (``nextPage`` URL in the response body),
    a configurable tag-to-channel map, and 429/401 error handling.
    """

    vendor: ClassVar[str] = "hoogendoorn"

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._api_key: str = ""
        self._base_url: str = ""
        self._installation_id: str = ""
        # Resolved channel map (DEFAULT_CHANNEL_MAP + params override).
        self._channel_map: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Validate params, load API key from env, build HTTP client."""
        params = self._config.params
        try:
            self._base_url = str(params["base_url"]).rstrip("/")
            api_key_env = str(params["api_key_env"])
            self._installation_id = str(params["installation_id"])
        except KeyError as exc:
            raise DataSourceConnectionError(
                f"Hoogendoorn config missing required param: {exc}"
            ) from exc

        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise DataSourceConnectionError(
                f"Hoogendoorn API key env var {api_key_env!r} is not set or empty"
            )
        self._api_key = api_key

        custom_map: dict[str, str] = dict(params.get("channel_map") or {})
        self._channel_map = {**DEFAULT_CHANNEL_MAP, **custom_map}

        # Guard against supervised restarts leaking the previous HTTP client.
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Stop the poll loop and close the HTTP client."""
        await super().close()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch_since(self, since: datetime | None) -> list[SourceReading]:
        """Poll the LetsGrow measurements endpoint with a since cursor.

        Handles pagination (``nextPage`` URL), 429 rate-limiting, and a
        single 401 guard — API keys do not rotate automatically, so a
        401 is logged as a configuration error and re-raised.
        """
        if self._client is None:
            raise DataSourceConnectionError("Hoogendoorn connector is not connected")

        query: dict[str, str] = {"installationId": self._installation_id}
        if since is not None:
            query["since"] = since.isoformat()

        readings: list[SourceReading] = []
        next_url: str | None = f"{self._base_url}{_MEASUREMENTS_PATH}"
        first_page = True

        while next_url is not None:
            response = await self._request(
                next_url, params=query if first_page else None
            )
            first_page = False

            payload: dict[str, Any] = response.json()
            readings.extend(self._parse_values(payload.get("data", [])))

            raw_next = payload.get("nextPage")
            if raw_next:
                next_url = (
                    raw_next
                    if str(raw_next).startswith("http")
                    else f"{self._base_url}{raw_next}"
                )
            else:
                next_url = None

        return readings

    async def _request(
        self,
        url: str,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET with API-key auth; handles 429 (Retry-After) and logs 401."""
        assert self._client is not None
        headers = {_API_KEY_HEADER: self._api_key}
        response = await self._client.get(url, params=params, headers=headers)

        if response.status_code == 429:
            # Retry-After may be an integer seconds value or an HTTP-date string.
            # Parse defensively: fall back to the default if conversion fails.
            raw_retry = response.headers.get("Retry-After", "")
            try:
                wait: float = float(raw_retry)
            except (ValueError, TypeError):
                wait = _DEFAULT_RETRY_AFTER
            logger.info("Hoogendoorn 429 — waiting %.0f s", wait)
            await asyncio.sleep(wait)
            response = await self._client.get(url, params=params, headers=headers)

        if response.status_code == 401:
            logger.error(
                "Hoogendoorn 401 — API key may be invalid; check env var %r",
                self._config.params.get("api_key_env", "unknown"),
            )

        response.raise_for_status()
        return response

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_values(self, values: list[Any]) -> list[SourceReading]:
        """Map LetsGrow measurement dicts to :class:`SourceReading` objects.

        Unknown tags are skipped (logged at DEBUG).  Entries missing a
        timestamp or with non-finite values are also skipped.

        Each item produces at most one ``SourceReading`` with one channel,
        allowing the :class:`~src.live.merge.ChannelMerger` to resolve
        priorities across sources on a per-channel basis.
        """
        readings: list[SourceReading] = []
        allowed = set(self._config.channels)

        for item in values:
            tag: str = str(item.get("tag", ""))
            canonical = self._channel_map.get(tag)
            if canonical is None:
                logger.debug(
                    "Hoogendoorn: unknown tag %r — add to channel_map if needed",
                    tag,
                )
                continue

            if canonical not in allowed:
                continue

            raw_ts = item.get("timestamp") or item.get("time")
            ts = parse_iso_timestamp(raw_ts)
            if ts is None:
                logger.debug(
                    "Hoogendoorn: missing/invalid timestamp for tag %r — skipping",
                    tag,
                )
                continue

            raw_val = item.get("value")
            if raw_val is None:
                continue
            try:
                fval = float(raw_val)
            except (TypeError, ValueError):
                logger.debug(
                    "Hoogendoorn: non-numeric value %r for tag %r — skipping",
                    raw_val,
                    tag,
                )
                continue

            if not math.isfinite(fval):
                continue

            lo, hi = CHANNEL_BOUNDS[canonical]
            clamped = max(lo, min(hi, fval))

            readings.append(
                SourceReading(
                    source_id=self._config.source_id,
                    vendor=self.vendor,
                    tenant_id=self._config.tenant_id,
                    greenhouse_id=self._config.greenhouse_id,
                    timestamp=ts,
                    channels={canonical: clamped},
                )
            )

        return readings

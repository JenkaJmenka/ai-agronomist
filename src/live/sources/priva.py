"""Priva SCADA connector for live greenhouse monitoring.

Connects to the Priva REST API using OAuth2 client-credentials, polls
the metrics-history endpoint with a since-cursor, and emits
:class:`~src.live.events.SourceReading` objects for the canonical four
greenhouse channels.

Configuration (``SourceConfig.params``)
-----------------------------------------
``base_url``
    Priva API root, e.g. ``https://api.priva.com``.
``client_id``
    OAuth2 client identifier.
``client_secret_env``
    **Name** of the environment variable holding the client secret.
    The secret is never stored in config literals.
``installation_id``
    Priva installation identifier.
``zone_id``
    Zone within the installation to poll.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime
from typing import Any, ClassVar

import httpx

from src.live.events import CHANNEL_BOUNDS, SourceReading
from src.live.sources.base import SourceConfig
from src.live.sources.polling import PollingSourceConnector, parse_iso_timestamp
from src.vendors.base import DataSourceConnectionError

logger = logging.getLogger("live.sources.priva")

# Raw Priva field names → canonical channel names.
# Mirrors the column renaming in src/vendors/priva_client.py (DataFrame
# adapter). Defined here to avoid a pandas dependency in the live pipeline.
_FIELD_MAP: dict[str, str] = {
    "ambient_temp": "temperature",
    "rel_humidity": "humidity",
    "co2_level": "co2",
    "radiation_lux": "light",
}

_OAUTH_PATH: str = "/oauth/token"
_METRICS_HISTORY_PATH: str = "/v1/metrics/history"

# Proactively refresh the token this many seconds before it expires.
_TOKEN_REFRESH_MARGIN_SECONDS: float = 60.0

# Default Retry-After delay when the header is absent from a 429 response.
_DEFAULT_RETRY_AFTER: float = 60.0


class PrivaConnector(PollingSourceConnector):
    """Polls the Priva REST API for greenhouse sensor readings.

    Authentication uses OAuth2 client-credentials.  The access token is
    cached and proactively refreshed ``_TOKEN_REFRESH_MARGIN_SECONDS``
    before expiry; a 401 response triggers one reactive refresh.

    Supports paginated responses (``next`` link in the response body)
    and respects ``Retry-After`` on 429 responses.
    """

    vendor: ClassVar[str] = "priva"

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._access_token: str = ""
        # Monotonic time at which the cached token is considered expired.
        self._token_expires_at: float = 0.0
        self._base_url: str = ""
        self._client_id: str = ""
        self._client_secret: str = ""
        self._installation_id: str = ""
        self._zone_id: str = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Validate params, load secret from env, fetch initial token."""
        params = self._config.params
        try:
            self._base_url = str(params["base_url"]).rstrip("/")
            self._client_id = str(params["client_id"])
            secret_env = str(params["client_secret_env"])
            self._installation_id = str(params["installation_id"])
            self._zone_id = str(params["zone_id"])
        except KeyError as exc:
            raise DataSourceConnectionError(
                f"Priva config missing required param: {exc}"
            ) from exc

        secret = os.environ.get(secret_env)
        if not secret:
            raise DataSourceConnectionError(
                f"Priva secret env var {secret_env!r} is not set or empty"
            )
        self._client_secret = secret
        # Guard against supervised restarts leaking the previous HTTP client.
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._client = httpx.AsyncClient(timeout=30.0)

        try:
            await self._refresh_token()
        except Exception as exc:
            await self._client.aclose()
            self._client = None
            raise DataSourceConnectionError(
                f"Priva OAuth token fetch failed: {exc}"
            ) from exc

    async def close(self) -> None:
        """Stop the poll loop and close the HTTP client."""
        await super().close()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _refresh_token(self) -> None:
        """Request a new access token via the client-credentials grant."""
        assert self._client is not None, "connect() must be called first"
        response = await self._client.post(
            f"{self._base_url}{_OAUTH_PATH}",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        self._access_token = data["access_token"]
        expires_in = float(data.get("expires_in", 3600))
        self._token_expires_at = (
            time.monotonic() + expires_in - _TOKEN_REFRESH_MARGIN_SECONDS
        )
        logger.debug("Priva token refreshed; expires in %.0f s", expires_in)

    def _token_is_expiring(self) -> bool:
        return time.monotonic() >= self._token_expires_at

    async def _ensure_token(self) -> None:
        """Proactively refresh the token if it is near expiry."""
        if self._token_is_expiring():
            await self._refresh_token()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch_since(self, since: datetime | None) -> list[SourceReading]:
        """Poll the Priva metrics-history endpoint with a since cursor.

        Handles pagination (``next`` link), 429 rate-limiting, and a
        single reactive 401 token refresh.
        """
        if self._client is None:
            raise DataSourceConnectionError("Priva connector is not connected")

        await self._ensure_token()

        query: dict[str, str] = {
            "installationId": self._installation_id,
            "zoneId": self._zone_id,
        }
        if since is not None:
            query["since"] = since.isoformat()

        readings: list[SourceReading] = []
        next_url: str | None = f"{self._base_url}{_METRICS_HISTORY_PATH}"
        first_page = True

        while next_url is not None:
            response = await self._request(
                next_url, params=query if first_page else None
            )
            first_page = False

            payload: dict[str, Any] = response.json()
            readings.extend(self._parse_metrics(payload.get("metrics", [])))

            raw_next = payload.get("next")
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
        """GET with bearer auth; handles 429 (Retry-After) and one 401 refresh."""
        assert self._client is not None
        headers = self._auth_headers()
        response = await self._client.get(url, params=params, headers=headers)

        if response.status_code == 429:
            # Retry-After may be an integer seconds value or an HTTP-date string.
            # Parse defensively: fall back to the default if conversion fails.
            raw_retry = response.headers.get("Retry-After", "")
            try:
                wait: float = float(raw_retry)
            except (ValueError, TypeError):
                wait = _DEFAULT_RETRY_AFTER
            logger.info("Priva 429 — waiting %.0f s (Retry-After)", wait)
            await asyncio.sleep(wait)
            response = await self._client.get(url, params=params, headers=headers)

        if response.status_code == 401:
            logger.info("Priva 401 — performing reactive token refresh")
            await self._refresh_token()
            headers = self._auth_headers()
            response = await self._client.get(url, params=params, headers=headers)

        response.raise_for_status()
        return response

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_metrics(self, metrics: list[Any]) -> list[SourceReading]:
        """Map Priva metric dicts to :class:`SourceReading` objects.

        Skips entries with missing/invalid timestamps, non-finite values,
        or no channels that overlap with the configured channel list.
        """
        readings: list[SourceReading] = []
        allowed = set(self._config.channels)

        for metric in metrics:
            raw_ts = metric.get("time") or metric.get("timestamp")
            ts = parse_iso_timestamp(raw_ts)
            if ts is None:
                logger.debug("Priva: metric missing timestamp — skipping: %r", metric)
                continue

            channels: dict[str, float] = {}
            for raw_key, canonical in _FIELD_MAP.items():
                if canonical not in allowed:
                    continue
                raw_val = metric.get(raw_key)
                if raw_val is None:
                    continue
                try:
                    fval = float(raw_val)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(fval):
                    continue
                lo, hi = CHANNEL_BOUNDS[canonical]
                channels[canonical] = max(lo, min(hi, fval))

            if not channels:
                continue

            readings.append(
                SourceReading(
                    source_id=self._config.source_id,
                    vendor=self.vendor,
                    tenant_id=self._config.tenant_id,
                    greenhouse_id=self._config.greenhouse_id,
                    timestamp=ts,
                    channels=channels,
                )
            )

        return readings

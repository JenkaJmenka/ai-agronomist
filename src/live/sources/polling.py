"""Abstract polling base connector for live SCADA/sensor data sources.

Concrete polling sources implement ``fetch_since`` and inherit the full
poll loop (interval sleep, since-cursor advance, deduplication, exponential
back-off on repeated errors, and clean ``close()``).
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from src.live.events import SourceReading
from src.live.sources.base import LiveSourceConnector, SourceConfig

logger = logging.getLogger("live.sources.polling")

# Back-off cap when fetch_since raises repeatedly (5 minutes).
_MAX_BACKOFF_SECONDS: float = 300.0


class PollingSourceConnector(LiveSourceConnector):
    """Abstract base for connectors that poll a REST/HTTP endpoint.

    Subclasses implement :meth:`fetch_since`.  :meth:`stream` is provided
    here and handles:

    - Polling every ``config.poll_interval_seconds``
    - Advancing the ``since`` cursor to the latest observed timestamp
    - Deduplicating readings at the cursor boundary (inclusive re-delivery)
    - Exponential back-off (doubling, capped at 5 min) on consecutive errors
    - Clean shutdown via :meth:`close`
    """

    def __init__(self, config: SourceConfig) -> None:
        super().__init__(config)
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Abstract contract for subclasses
    # ------------------------------------------------------------------

    @abstractmethod
    async def fetch_since(self, since: datetime | None) -> list[SourceReading]:
        """Fetch readings from the source newer than *since*.

        Parameters
        ----------
        since:
            Exclusive lower bound (or ``None`` for the first fetch).
            Some vendors use inclusive semantics — the polling base
            deduplicates the cursor boundary automatically.

        Returns
        -------
        list[SourceReading]
            May be empty if no new readings are available.
        """

    # ------------------------------------------------------------------
    # Default connect — override in subclasses that need auth/sessions
    # ------------------------------------------------------------------

    async def connect(self) -> None:  # noqa: D401
        """No-op default; override to establish HTTP sessions or auth."""

    # ------------------------------------------------------------------
    # Stream loop
    # ------------------------------------------------------------------

    async def stream(self) -> AsyncIterator[SourceReading]:  # type: ignore[override]
        """Yield readings from periodic ``fetch_since`` calls.

        Runs until :meth:`close` is called.  On consecutive errors the
        loop backs off exponentially (1 s → 2 → … → 300 s) and resets
        to 1 s after the next successful fetch.
        """
        since: datetime | None = None
        # Timestamps equal to the cursor that may be re-delivered by vendors
        # using inclusive (>=) since-semantics.
        boundary: set[datetime] = set()
        backoff: float = 1.0

        while not self._closed:
            try:
                readings = await self.fetch_since(since)
                backoff = 1.0  # reset on success

                if readings:
                    ordered = sorted(readings, key=lambda r: r.timestamp)
                    new_readings = [r for r in ordered if r.timestamp not in boundary]

                    for reading in new_readings:
                        yield reading

                    if new_readings:
                        new_since = max(r.timestamp for r in new_readings)
                        since = new_since
                        # Keep only boundary timestamps that may be re-sent next
                        # fetch.  Earlier timestamps are excluded by the cursor.
                        boundary = {
                            r.timestamp
                            for r in new_readings
                            if r.timestamp == new_since
                        }

                await asyncio.sleep(self._config.poll_interval_seconds)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Polling source %r fetch error: %s — backing off %.1f s",
                    self._config.source_id,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, _MAX_BACKOFF_SECONDS)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Signal the stream loop to stop at the next iteration."""
        self._closed = True


# ------------------------------------------------------------------
# Shared helpers used by priva.py and hoogendoorn.py
# ------------------------------------------------------------------


def parse_iso_timestamp(raw: Any) -> datetime | None:
    """Parse an ISO-8601 string to a timezone-aware UTC datetime.

    Returns ``None`` on parse failure so callers can skip bad entries.
    Handles both ``Z`` suffix and ``+00:00`` offset notation.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return None

"""Persistence batcher for live monitoring readings.

Buffers ``LiveReadingEvent`` objects in memory and bulk-inserts them into
``SensorReading`` on a configurable flush interval.

The batcher is cancellation-safe: when its ``run()`` coroutine is cancelled
(e.g. by ``LiveRuntime.stop()``), it performs a final flush before
re-raising ``CancelledError``.

Duplicate detection
-------------------
The ``(tenant_id, greenhouse_id, timestamp)`` unique constraint on
``sensor_readings`` is enforced via ``INSERT ... ON CONFLICT DO NOTHING``
using the SQLAlchemy dialect-specific insert.  Duplicate rows are silently
dropped rather than overwriting previously persisted data.

NaN / Infinity guard
--------------------
``LiveReadingEvent.channels`` values are already validated as finite by the
Pydantic model, but ``sanitize_channels`` is applied as a second defence to
guard the ``anomaly_score`` path and any future callers that bypass the
Pydantic constructor.

Incomplete-row policy
---------------------
``SensorReading`` has four non-nullable channel columns (``temperature``,
``humidity``, ``co2``, ``light``).  When a merged bucket contains only a
subset of those channels — e.g. during warm-up, single-channel buckets, or
CO2-source dropout — the missing channels cannot be represented without
fabricating a ``0.0`` sentinel value that would poison training/analytics data
(a ``0.0`` CO2 reading is indistinguishable from a real fault).

Policy: rows missing **any** of the four canonical channels are **skipped**
(not written to the DB).  The count of skipped rows is tracked in
``PersistenceBatcher.skipped_incomplete_count`` and logged at WARNING level.
Complete rows (all four channels present after NaN sanitization) are
persisted normally.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects import sqlite as _sqlite_dialect

from src.config import LIVE_PERSIST_FLUSH_SECONDS
from src.db.models import SensorReading
from src.live.events import LiveReadingEvent, sanitize_channels

logger = logging.getLogger("live.persistence")

# Hard cap on in-memory buffered events.
_BUFFER_CAP: int = 10_000

# The four non-nullable channel columns in SensorReading.  A row missing any
# of these cannot be persisted without fabricating data, so it is skipped.
_CANONICAL_CHANNELS: frozenset[str] = frozenset({"temperature", "humidity", "co2", "light"})

# Dialect-specific INSERT constructors (mirrors src/db/upsert.py pattern).
_INSERT_BY_DIALECT: Dict[str, Callable[..., Any]] = {
    "postgresql": postgresql.insert,
    "sqlite": _sqlite_dialect.insert,
}

# Conflict target matching the UniqueConstraint in SensorReading.
_CONFLICT_COLS: list[str] = ["tenant_id", "greenhouse_id", "timestamp"]


def _event_to_row(event: LiveReadingEvent) -> Optional[Dict[str, Any]]:
    """Convert a ``LiveReadingEvent`` to a ``SensorReading`` column dict.

    Completeness is checked on the **original** channels dict (before NaN
    sanitization).  If any canonical channel key is absent the event is
    incomplete and ``None`` is returned — fabricating ``0.0`` for a channel
    that was never provided would poison training data (see module docstring).

    For channels that **are** present but carry a non-finite value (NaN /
    Inf), ``sanitize_channels`` strips the bad value and the fallback ``0.0``
    is written.  This is acceptable: the sensor reported a value (just invalid)
    rather than never reporting at all.
    """
    if not _CANONICAL_CHANNELS.issubset(event.channels.keys()):
        return None
    safe_channels = sanitize_channels(event.channels)
    return {
        "tenant_id": event.tenant_id,
        "greenhouse_id": event.greenhouse_id,
        "timestamp": event.timestamp,
        "temperature": safe_channels.get("temperature", 0.0),
        "humidity": safe_channels.get("humidity", 0.0),
        "co2": safe_channels.get("co2", 0.0),
        "light": safe_channels.get("light", 0.0),
        "anomaly_score": event.anomaly_score,
    }


class PersistenceBatcher:
    """Buffer live readings and bulk-insert them periodically.

    Parameters
    ----------
    flush_interval:
        Seconds between automatic flushes (default: ``LIVE_PERSIST_FLUSH_SECONDS``).
    session_factory:
        Async session factory; defaults to the global ``AsyncSessionLocal``.
        Inject a test factory to keep tests hermetic.
    """

    def __init__(
        self,
        flush_interval: float = LIVE_PERSIST_FLUSH_SECONDS,
        *,
        session_factory: Optional[Any] = None,
    ) -> None:
        self._flush_interval = flush_interval
        self._buffer: Deque[LiveReadingEvent] = deque()
        self._dropped_count: int = 0
        self._skipped_incomplete_count: int = 0
        self._task: Optional[asyncio.Task[None]] = None
        if session_factory is None:
            from src.db.session import AsyncSessionLocal
            session_factory = AsyncSessionLocal
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add(self, event: LiveReadingEvent) -> None:
        """Append *event* to the buffer, dropping the oldest if at capacity."""
        if len(self._buffer) >= _BUFFER_CAP:
            self._buffer.popleft()
            self._dropped_count += 1
            logger.warning(
                "Persistence buffer full — dropped oldest event "
                "(total dropped so far: %d)",
                self._dropped_count,
            )
        self._buffer.append(event)

    async def run(self) -> None:
        """Background flush loop.

        Sleeps *flush_interval* seconds between flushes.  On cancellation,
        performs a final flush of any remaining buffered events before
        re-raising ``CancelledError``.
        """
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                await self._flush()
        except asyncio.CancelledError:
            await self._flush()
            raise

    async def close(self) -> None:
        """Cancel the background task (if started) and do a final flush."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.error(
                    "Persistence background task raised on close: %s",
                    exc,
                    exc_info=True,
                )
            self._task = None
        await self._flush()

    def start_background_task(self) -> None:
        """Schedule the flush loop as a background asyncio task.

        Useful for standalone use; in ``LiveRuntime`` the supervisor wraps
        ``run()`` directly.
        """
        self._task = asyncio.create_task(self.run(), name="persistence_flush")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _flush(self) -> None:
        """Drain the buffer and bulk-insert rows into ``SensorReading``."""
        if not self._buffer:
            return

        # Atomically drain the buffer so new events can accumulate during I/O.
        to_flush: list[LiveReadingEvent] = list(self._buffer)
        self._buffer.clear()

        # Skip events that lack all four canonical channels (see module docstring).
        rows: list[Dict[str, Any]] = []
        for ev in to_flush:
            row = _event_to_row(ev)
            if row is None:
                self._skipped_incomplete_count += 1
            else:
                rows.append(row)
        if self._skipped_incomplete_count and len(rows) < len(to_flush):
            logger.warning(
                "Persistence: skipped %d incomplete event(s) missing canonical channels "
                "(total skipped so far: %d)",
                len(to_flush) - len(rows),
                self._skipped_incomplete_count,
            )

        if not rows:
            return

        try:
            async with self._session_factory() as session:
                dialect_name: str = session.bind.dialect.name
                insert_fn = _INSERT_BY_DIALECT.get(dialect_name)
                if insert_fn is None:
                    raise NotImplementedError(
                        f"Persistence batcher: unsupported dialect {dialect_name!r}"
                    )
                stmt = (
                    insert_fn(SensorReading)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=_CONFLICT_COLS)
                )
                await session.execute(stmt)
                await session.commit()
                logger.debug("Persistence flush: %d rows written.", len(rows))
        except Exception as exc:
            logger.error(
                "Persistence flush failed for %d rows — buffering for retry: %s",
                len(rows),
                exc,
                exc_info=True,
            )
            # Return rows to the front of the buffer for the next flush cycle.
            # The cap still applies; excess rows are silently discarded.
            for ev in reversed(to_flush):
                if len(self._buffer) < _BUFFER_CAP:
                    self._buffer.appendleft(ev)
                else:
                    self._dropped_count += 1

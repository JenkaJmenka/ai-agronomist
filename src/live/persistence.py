"""Persistence batcher for live monitoring readings.

Buffers ``LiveReadingEvent`` objects in memory and bulk-inserts them into
``SensorReading`` **and** ``RawReading`` on a configurable flush interval.

The batcher is cancellation-safe: when its ``run()`` coroutine is cancelled
(e.g. by ``LiveRuntime.stop()``), it performs a final flush before
re-raising ``CancelledError``.

Dual-table contract
-------------------
Each flush writes to two tables in the **same session/transaction**:

1. ``sensor_readings`` (``SensorReading``) — the four canonical float columns
   (temperature, humidity, co2, light) plus ``anomaly_score``.  Only written
   for complete buckets (see "Incomplete-row policy" below).

2. ``raw_readings`` (``RawReading``) — the **full** ``event.channels`` dict
   (canonical + extras + vpd) stored as a JSON ``data`` column with
   ``source="live"``.  Written for *any* event whose sanitized channel dict
   is non-empty; no completeness requirement.

The raw row's ``data`` must be the **complete** channel dict — not extras
only.  ``fetch_merged_readings`` uses raw rows as-is when they exist; an
extras-only dict would drop temperature/humidity/co2/light from the
monitoring view for live data, breaking the main chart.

This is the same mechanism used by the CSV upload flow: the read path
(``fetch_merged_readings``) already merges ``RawReading`` rows with canonical
anomaly scores, so extra channels (soil_moisture, root_zone_temp, vpd) surface
in ``/api/readings/*`` responses with zero read-path changes.

Conflict semantics
------------------
Both tables enforce ``ON CONFLICT DO NOTHING`` on
``(tenant_id, greenhouse_id, timestamp)``.  If a CSV upload's timestamps
collide with live raw rows already written here, first-writer-wins — the same
behaviour the canonical table has today.  This is acceptable: partial-flush
retries are idempotent via both unique constraints.

Duplicate detection
-------------------
The ``(tenant_id, greenhouse_id, timestamp)`` unique constraint on both tables
is enforced via ``INSERT ... ON CONFLICT DO NOTHING`` using the SQLAlchemy
dialect-specific insert.  Duplicate rows are silently dropped rather than
overwriting previously persisted data.

NaN / Infinity guard (raw rows)
--------------------------------
``sanitize_channels`` strips non-finite values before the dict reaches the
JSON column.  Do **not** use a ``.get(k, 0.0)`` fallback here — that would
write a ``0.0`` sentinel for a channel that was never provided, corrupting the
full-fidelity record.  The known SQLite/Postgres NaN gap (SQLite JSON accepts
NaN; Postgres 500s on insert) makes this guard mandatory.

NaN / Infinity guard (canonical rows)
--------------------------------------
``LiveReadingEvent.channels`` values are already validated as finite by the
Pydantic model, but ``sanitize_channels`` is applied as a second defence to
guard the ``anomaly_score`` path and any future callers that bypass the
Pydantic constructor.  For canonical rows, NaN in a **present** channel is
replaced with ``0.0`` via ``.get(k, 0.0)`` — this is acceptable because the
sensor reported a value (just invalid) rather than never reporting at all.

Incomplete-row policy (canonical only)
----------------------------------------
``SensorReading`` has four non-nullable channel columns (``temperature``,
``humidity``, ``co2``, ``light``).  When a merged bucket contains only a
subset of those channels — e.g. during warm-up, single-channel buckets, or
CO2-source dropout — the missing channels cannot be represented without
fabricating a ``0.0`` sentinel value that would poison training/analytics data
(a ``0.0`` CO2 reading is indistinguishable from a real fault).

Policy: rows missing **any** of the four canonical channels are **skipped**
from ``sensor_readings`` (not written to the DB).  The count of skipped rows
is tracked in ``PersistenceBatcher.skipped_incomplete_count`` and logged at
WARNING level.  The corresponding raw row **is still written** to
``raw_readings`` (extras-only buckets are valid partial records).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects import sqlite as _sqlite_dialect

from src.config import LIVE_PERSIST_FLUSH_SECONDS
from src.db.models import RawReading, SensorReading
from src.live.events import LiveReadingEvent, sanitize_channels
from src.live.scope_control import is_live_scope_suppressed, suppress_live_scope, resume_live_scope

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
        # Provenance tag so live rows can be wiped without clearing CSV uploads.
        "source": "live",
    }


def _event_to_raw_row(event: LiveReadingEvent) -> Optional[Dict[str, Any]]:
    """Convert a ``LiveReadingEvent`` to a ``RawReading`` column dict.

    Writes the **full** channel dict (canonical + extras + vpd) — not
    extras-only.  The read path (``fetch_merged_readings``) uses raw rows as-is
    when they exist; an extras-only dict would drop temperature/humidity/co2/
    light from the monitoring view for live data.

    ``sanitize_channels`` strips non-finite values before the dict reaches the
    JSON column (mandatory NaN guard — see module docstring).  Do **not** apply
    a ``.get(k, 0.0)`` fallback here: a missing value should simply be absent
    from the dict, not fabricated.

    Returns ``None`` only when the sanitized dict is empty (all channels
    non-finite, or ``event.channels`` was empty).

    No completeness requirement: ``RawReading`` has no non-nullable channel
    columns; partial buckets are valid full-fidelity records.
    """
    safe = sanitize_channels(event.channels)
    if not safe:
        return None
    return {
        "tenant_id": event.tenant_id,
        "greenhouse_id": event.greenhouse_id,
        "timestamp": event.timestamp,
        "source": "live",
        "data": safe,
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

    def suppress_scope(self, tenant_id: str, greenhouse_id: str) -> None:
        """Stop persisting live events for a tenant/greenhouse and drop buffered ones."""
        suppress_live_scope(tenant_id, greenhouse_id)
        key = (tenant_id, greenhouse_id)
        kept = deque(
            ev
            for ev in self._buffer
            if (ev.tenant_id, ev.greenhouse_id) != key
        )
        dropped = len(self._buffer) - len(kept)
        self._buffer = kept
        if dropped:
            logger.info(
                "Persistence: dropped %d buffered event(s) for suppressed scope %s/%s",
                dropped,
                tenant_id,
                greenhouse_id,
            )

    def resume_scope(self, tenant_id: str, greenhouse_id: str) -> None:
        """Allow live persistence again for a tenant/greenhouse (e.g. after Go Live)."""
        resume_live_scope(tenant_id, greenhouse_id)

    def is_scope_suppressed(self, tenant_id: str, greenhouse_id: str) -> bool:
        return is_live_scope_suppressed(tenant_id, greenhouse_id)

    async def add(self, event: LiveReadingEvent) -> None:
        """Append *event* to the buffer, dropping the oldest if at capacity."""
        if is_live_scope_suppressed(event.tenant_id, event.greenhouse_id):
            return
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
        """Drain the buffer and bulk-insert rows into ``SensorReading`` and ``RawReading``.

        Both inserts run in the **same** session/transaction so a mid-flush
        failure leaves neither table partially committed.  On exception all
        drained events are requeueed for the next flush cycle (idempotent via
        both unique constraints).
        """
        if not self._buffer:
            return

        # Atomically drain the buffer so new events can accumulate during I/O.
        to_flush: list[LiveReadingEvent] = list(self._buffer)
        self._buffer.clear()

        # Build both row lists in the same drain pass.
        rows: list[Dict[str, Any]] = []
        raw_rows: list[Dict[str, Any]] = []
        for ev in to_flush:
            canonical = _event_to_row(ev)
            if canonical is None:
                self._skipped_incomplete_count += 1
            else:
                rows.append(canonical)
            raw = _event_to_raw_row(ev)
            if raw is not None:
                raw_rows.append(raw)

        if self._skipped_incomplete_count and len(rows) < len(to_flush):
            logger.warning(
                "Persistence: skipped %d incomplete event(s) missing canonical channels "
                "(total skipped so far: %d)",
                len(to_flush) - len(rows),
                self._skipped_incomplete_count,
            )

        if not rows and not raw_rows:
            return

        try:
            async with self._session_factory() as session:
                dialect_name: str = session.bind.dialect.name
                insert_fn = _INSERT_BY_DIALECT.get(dialect_name)
                if insert_fn is None:
                    raise NotImplementedError(
                        f"Persistence batcher: unsupported dialect {dialect_name!r}"
                    )
                if rows:
                    stmt = (
                        insert_fn(SensorReading)
                        .values(rows)
                        .on_conflict_do_nothing(index_elements=_CONFLICT_COLS)
                    )
                    await session.execute(stmt)
                if raw_rows:
                    raw_stmt = (
                        insert_fn(RawReading)
                        .values(raw_rows)
                        .on_conflict_do_nothing(index_elements=_CONFLICT_COLS)
                    )
                    await session.execute(raw_stmt)
                await session.commit()
                logger.debug(
                    "Persistence flush: %d canonical row(s), %d raw row(s) written.",
                    len(rows),
                    len(raw_rows),
                )
        except Exception as exc:
            logger.error(
                "Persistence flush failed (%d canonical, %d raw rows) — buffering for retry: %s",
                len(rows),
                len(raw_rows),
                exc,
                exc_info=True,
            )
            # Return events to the front of the buffer for the next flush cycle.
            # The cap still applies; excess events are silently discarded.
            for ev in reversed(to_flush):
                if len(self._buffer) < _BUFFER_CAP:
                    self._buffer.appendleft(ev)
                else:
                    self._dropped_count += 1

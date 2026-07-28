"""ChannelMerger -- bucket-aligned multi-source reading merger.

One instance per (tenant_id, greenhouse_id).  Connector tasks push
``SourceReading`` objects via :meth:`submit`.  The merger aligns readings
into time buckets, resolves per-channel conflicts by source priority,
and emits ``LiveReadingEvent`` via an async callback.

This module does **not** import ``LiveHub`` or any database module -- the
runtime wires the ``on_reading`` / ``on_status`` callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from src.config import LIVE_MERGE_GRACE_SECONDS
from src.live.events import LiveReadingEvent, SourceReading, SourceStatusEvent
from src.live.sources.base import SourceConfig

logger = logging.getLogger("live.merge")

# Health thresholds (multiples of a source's poll_interval_seconds)
_DEGRADED_MULTIPLIER: float = 3.0
_OFFLINE_MULTIPLIER: float = 10.0


class _BucketEntry:
    """Accumulates per-channel readings within one time bucket."""

    __slots__ = ("channels", "sources", "priorities")

    def __init__(self) -> None:
        self.channels: Dict[str, float] = {}
        self.sources: Dict[str, str] = {}
        self.priorities: Dict[str, int] = {}

    def accept(
        self,
        channel: str,
        value: float,
        source_id: str,
        priority: int,
    ) -> None:
        """Accept a channel value; lowest priority wins, ties -> last-write-wins."""
        existing = self.priorities.get(channel)
        if existing is not None and priority > existing:
            return  # higher number = lower priority = loses
        self.channels[channel] = value
        self.sources[channel] = source_id
        self.priorities[channel] = priority


class ChannelMerger:
    """Bucket-aligned multi-source merger for a single scope.

    Parameters
    ----------
    tenant_id, greenhouse_id:
        Scope identifiers.
    emit_interval:
        Bucket width in seconds.
    source_configs:
        List of :class:`SourceConfig` for priority / interval metadata.
    on_reading:
        Async callback for each merged :class:`LiveReadingEvent`.
        ``seq`` is set to ``""`` -- the SSE layer uses the Redis entry ID
        as the authoritative ``id:`` field.
    on_status:
        Async callback for source health transitions.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        greenhouse_id: str,
        emit_interval: float,
        source_configs: List[SourceConfig],
        on_reading: Callable[[LiveReadingEvent], Awaitable[None]],
        on_status: Callable[[SourceStatusEvent], Awaitable[None]],
        grace_seconds: float = LIVE_MERGE_GRACE_SECONDS,
    ) -> None:
        self._tenant_id = tenant_id
        self._greenhouse_id = greenhouse_id
        self._emit_interval = emit_interval
        self._grace_seconds = grace_seconds
        self._on_reading = on_reading
        self._on_status = on_status

        # source_id -> (priority, poll_interval)
        self._source_meta: Dict[str, Tuple[int, float]] = {
            sc.source_id: (sc.priority, sc.poll_interval_seconds)
            for sc in source_configs
        }
        self._source_vendors: Dict[str, str] = {
            sc.source_id: sc.vendor for sc in source_configs
        }

        self.inbox: asyncio.Queue[SourceReading] = asyncio.Queue()

        # Bucket state
        self._buckets: Dict[int, _BucketEntry] = {}
        self._bucket_ts: Dict[int, datetime] = {}
        self._emitted_watermark: int = -1
        self.dropped_late_count: int = 0
        self.dropped_future_count: int = 0

        # Health tracking
        now_mono = time.monotonic()
        self._health_status: Dict[str, str] = {
            sid: "online" for sid in self._source_meta
        }
        self._last_seen_mono: Dict[str, float] = {
            sid: now_mono for sid in self._source_meta
        }
        self._last_seen_dt: Dict[str, Optional[datetime]] = {
            sid: None for sid in self._source_meta
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit(self, reading: SourceReading) -> None:
        """Push a reading into the merger's inbox queue."""
        await self.inbox.put(reading)

    def current_statuses(self) -> Dict[str, str]:
        """Return current health status for each registered source."""
        return dict(self._health_status)

    def full_statuses(self) -> List[Dict[str, Optional[str]]]:
        """Return full status dicts for each registered source.

        Each dict contains ``source_id``, ``vendor``, ``status``, and
        ``last_seen`` (ISO-8601 string or ``None`` if never seen).  This
        is the canonical shape required by the ``/api/live/status`` endpoint
        and the ``source-status`` SSE event schema.
        """
        result: List[Dict[str, Optional[str]]] = []
        for sid, status in self._health_status.items():
            last_dt = self._last_seen_dt.get(sid)
            result.append(
                {
                    "source_id": sid,
                    "vendor": self._source_vendors.get(sid, "unknown"),
                    "status": status,
                    "last_seen": last_dt.isoformat() if last_dt is not None else None,
                }
            )
        return result

    async def run(self) -> None:
        """Main merger loop.  Cancellation-safe (final flush on cancel)."""
        try:
            while True:
                self._drain_inbox()
                await self._flush_ready_buckets()
                await self._sweep_health()
                await asyncio.sleep(self._emit_interval / 4.0)
        except asyncio.CancelledError:
            # Always attempt a final flush so buffered sensor data is preserved on
            # clean shutdown.  If the flush itself raises (Redis unavailable, DB
            # blip, …), log the failure and continue — the CancelledError MUST
            # still be re-raised so the supervised() wrapper does not mistake this
            # for a crash and restart the task, which would cause LiveRuntime.stop()
            # to hang indefinitely (asyncio.gather never returns).
            try:
                await self._flush_all_buckets()
            except Exception as flush_exc:
                logger.error(
                    "ChannelMerger(%s/%s): flush failed during shutdown"
                    " — some buffered events may be lost: %s",
                    self._tenant_id,
                    self._greenhouse_id,
                    flush_exc,
                    exc_info=True,
                )
            raise

    # ------------------------------------------------------------------
    # Inbox processing
    # ------------------------------------------------------------------

    def _drain_inbox(self) -> None:
        """Process all queued readings without blocking."""
        while not self.inbox.empty():
            try:
                reading = self.inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._ingest(reading)

    def _ingest(self, reading: SourceReading) -> None:
        """Assign a reading to the correct time bucket."""
        # Drop readings with far-future timestamps to prevent unbounded
        # bucket growth (bucket would never flush).
        now_epoch = time.time()
        max_future = now_epoch + self._grace_seconds + self._emit_interval
        if reading.timestamp.timestamp() > max_future:
            self.dropped_future_count += 1
            return

        bucket_idx = int(reading.timestamp.timestamp() // self._emit_interval)

        if bucket_idx <= self._emitted_watermark:
            self.dropped_late_count += 1
            return

        source_id = reading.source_id
        priority = self._source_meta.get(source_id, (100, 5.0))[0]

        bucket = self._buckets.get(bucket_idx)
        if bucket is None:
            bucket = _BucketEntry()
            self._buckets[bucket_idx] = bucket
            self._bucket_ts[bucket_idx] = reading.timestamp

        for channel, value in reading.channels.items():
            if not math.isfinite(value):
                continue
            bucket.accept(channel, value, source_id, priority)

        # Update source health bookkeeping
        self._last_seen_mono[source_id] = time.monotonic()
        self._last_seen_dt[source_id] = reading.timestamp

    # ------------------------------------------------------------------
    # Bucket flushing
    # ------------------------------------------------------------------

    async def _flush_ready_buckets(self) -> None:
        """Emit buckets whose grace period has elapsed."""
        if not self._buckets:
            return

        now_epoch = time.time()
        ready: List[int] = []
        for idx in sorted(self._buckets):
            bucket_end = (idx + 1) * self._emit_interval
            if now_epoch >= bucket_end + self._grace_seconds:
                ready.append(idx)

        for idx in ready:
            await self._emit_bucket(idx)

    async def _flush_all_buckets(self) -> None:
        """Emit all remaining buckets (shutdown path)."""
        for idx in sorted(self._buckets):
            await self._emit_bucket(idx)

    async def _emit_bucket(self, bucket_idx: int) -> None:
        """Emit one bucket as a ``LiveReadingEvent``."""
        bucket = self._buckets.pop(bucket_idx, None)
        ts = self._bucket_ts.pop(bucket_idx, None)
        if bucket is None or not bucket.channels:
            return

        event = LiveReadingEvent(
            seq="",
            timestamp=ts or datetime.now(timezone.utc),
            tenant_id=self._tenant_id,
            greenhouse_id=self._greenhouse_id,
            channels=bucket.channels,
            sources=bucket.sources,
        )
        await self._on_reading(event)

        if bucket_idx > self._emitted_watermark:
            self._emitted_watermark = bucket_idx

    # ------------------------------------------------------------------
    # Source health monitoring
    # ------------------------------------------------------------------

    async def _sweep_health(self) -> None:
        """Detect source degraded/offline/online transitions."""
        now_mono = time.monotonic()

        for source_id in list(self._source_meta):
            interval = self._source_meta[source_id][1]
            elapsed = now_mono - self._last_seen_mono.get(source_id, 0.0)
            current = self._health_status.get(source_id, "online")

            if elapsed >= _OFFLINE_MULTIPLIER * interval:
                new_status = "offline"
            elif elapsed >= _DEGRADED_MULTIPLIER * interval:
                new_status = "degraded"
            else:
                new_status = "online"

            if new_status == current:
                continue

            self._health_status[source_id] = new_status
            vendor = self._source_vendors.get(source_id, "unknown")
            last_dt = self._last_seen_dt.get(source_id)

            await self._on_status(
                SourceStatusEvent(
                    source_id=source_id,
                    vendor=vendor,
                    status=new_status,  # type: ignore[arg-type]
                    last_seen=last_dt,
                )
            )

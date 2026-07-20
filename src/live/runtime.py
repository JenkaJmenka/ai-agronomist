"""LiveRuntime — supervised orchestrator for live monitoring data sources.

Builds one :class:`~src.live.merge.ChannelMerger` per scope
(``tenant_id``/``greenhouse_id`` pair) and wraps each long-running
coroutine (merger loop, connector streaming) in a restart-on-crash
supervisor with exponential back-off.

Usage
-----
::

    hub = get_live_hub()
    runtime = LiveRuntime.build_from_config(hub)
    await runtime.start()
    app.state.live_runtime = runtime
    ...
    await runtime.stop()

The scorer (``src.live.scoring.LiveScorer``) is imported **lazily**:
if the module is absent (parallel agent not yet delivered), a passthrough
is used and a warning is logged — the rest of the pipeline still runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from src.config import (
    LIVE_ANOMALY_THRESHOLD,
    LIVE_MERGE_GRACE_SECONDS,
    LIVE_PERSIST_ENABLED,
    LIVE_SOURCES_JSON,
    MOCK_SCADA_ENABLED,
    MOCK_SCADA_INTERVAL_SECONDS,
    MOCK_SCADA_SCOPES,
)
from src.live.derived import compute_vpd
from src.live.events import LiveReadingEvent, SourceStatusEvent
from src.live.hub import LiveHub
from src.live.merge import ChannelMerger
from src.live.persistence import PersistenceBatcher
from src.live.sources import CONNECTOR_TYPES
from src.live.sources.base import LiveSourceConnector, SourceConfig

logger = logging.getLogger("live.runtime")

# Exponential back-off cap for supervised restart loops (seconds).
_MAX_BACKOFF_SECONDS: float = 60.0


# ---------------------------------------------------------------------------
# Derived channels
# ---------------------------------------------------------------------------


def _attach_derived_vpd(event: LiveReadingEvent) -> None:
    """Add the derived VPD channel when temperature and humidity are present.

    Mutates ``event.channels``/``event.sources`` in place. The scorer only
    consumes the canonical ML channels, so VPD never affects scoring.
    """
    temperature = event.channels.get("temperature")
    humidity = event.channels.get("humidity")
    if temperature is None or humidity is None:
        return
    event.channels["vpd"] = compute_vpd(temperature, humidity)
    event.sources["vpd"] = "derived"


# ---------------------------------------------------------------------------
# Scope context (one per tenant/greenhouse pair)
# ---------------------------------------------------------------------------


@dataclass
class _ScopeContext:
    """All live components for a single tenant/greenhouse scope."""

    tenant_id: str
    greenhouse_id: str
    connectors: list[LiveSourceConnector]
    merger: ChannelMerger
    scorer: Any = field(default=None)  # LiveScorer | None


# ---------------------------------------------------------------------------
# Supervised task wrapper
# ---------------------------------------------------------------------------


_BACKOFF_RESET_THRESHOLD: float = 2.0 * _MAX_BACKOFF_SECONDS
"""If a coroutine ran longer than this before failing, reset backoff to
the floor (1.0 s) — the long healthy run indicates a transient failure,
not a persistent crash loop."""


async def _supervised(label: str, coro_fn: Any, *args: Any) -> None:
    """Run *coro_fn* in a restart loop with exponential back-off.

    Propagates ``CancelledError`` immediately so the supervisor shuts down
    cleanly when the enclosing ``asyncio.Task`` is cancelled.

    Backoff resets to the floor (1.0 s) when the coroutine ran longer than
    ``_BACKOFF_RESET_THRESHOLD`` (2 × ``_MAX_BACKOFF_SECONDS``) before
    failing — a long healthy run followed by a crash is transient, not a
    persistent fault.

    Parameters
    ----------
    label:
        Human-readable name used in log messages.
    coro_fn:
        Async callable to supervise.  Called with ``*args`` on each run.
    """
    backoff: float = 1.0
    while True:
        run_start = asyncio.get_event_loop().time()
        try:
            await coro_fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            run_duration = asyncio.get_event_loop().time() - run_start
            if run_duration >= _BACKOFF_RESET_THRESHOLD:
                backoff = 1.0
            logger.warning(
                "Live task %r crashed (%s) after %.1f s — restarting in %.1f s",
                label,
                exc,
                run_duration,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, _MAX_BACKOFF_SECONDS)


async def _run_connector(
    connector: LiveSourceConnector,
    merger: ChannelMerger,
) -> None:
    """Connect to a data source and stream readings into the merger."""
    await connector.connect()
    async for reading in connector.stream():
        await merger.submit(reading)


# ---------------------------------------------------------------------------
# LiveRuntime
# ---------------------------------------------------------------------------


class LiveRuntime:
    """Supervised orchestrator for all live monitoring data sources.

    Do not instantiate directly — use :meth:`build_from_config`.
    """

    def __init__(
        self,
        hub: LiveHub,
        scopes: list[_ScopeContext],
        batcher: Optional[PersistenceBatcher],
    ) -> None:
        self._hub = hub
        self._scopes = scopes
        self._batcher = batcher
        self._tasks: list[asyncio.Task[None]] = []
        # Index for fast scope lookup in source_statuses().
        self._mergers: dict[tuple[str, str], ChannelMerger] = {
            (sc.tenant_id, sc.greenhouse_id): sc.merger for sc in scopes
        }

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build_from_config(cls, hub: LiveHub) -> "LiveRuntime":
        """Build a ``LiveRuntime`` from environment config.

        Reads ``MOCK_SCADA_ENABLED``, ``MOCK_SCADA_SCOPES``, and
        ``LIVE_SOURCES_JSON``.  Unknown vendors in ``LIVE_SOURCES_JSON``
        are logged and skipped.
        """
        batcher = PersistenceBatcher() if LIVE_PERSIST_ENABLED else None

        # scope_key -> list of connectors
        scope_connectors: dict[tuple[str, str], list[LiveSourceConnector]] = {}

        if MOCK_SCADA_ENABLED:
            for raw_scope in MOCK_SCADA_SCOPES.split(","):
                raw_scope = raw_scope.strip()
                if ":" not in raw_scope:
                    logger.warning("Invalid MOCK_SCADA_SCOPES entry %r — skipping", raw_scope)
                    continue
                tenant_id, greenhouse_id = raw_scope.split(":", 1)
                tenant_id = tenant_id.strip()
                greenhouse_id = greenhouse_id.strip()
                if not tenant_id or not greenhouse_id:
                    logger.warning("Empty tenant or greenhouse in scope %r — skipping", raw_scope)
                    continue
                from src.live.sources.mock import build_mock_sources_for_scope
                connectors = list(build_mock_sources_for_scope(tenant_id, greenhouse_id))
                scope_connectors.setdefault((tenant_id, greenhouse_id), []).extend(connectors)

        if LIVE_SOURCES_JSON:
            try:
                source_defs = json.loads(LIVE_SOURCES_JSON)
                for sc_dict in source_defs:
                    try:
                        sc = SourceConfig(**sc_dict)
                    except Exception as exc:
                        logger.warning("Invalid SourceConfig in LIVE_SOURCES_JSON: %s — skipping", exc)
                        continue
                    connector_cls = CONNECTOR_TYPES.get(sc.vendor)
                    if connector_cls is None:
                        logger.warning(
                            "Unknown vendor %r for source %r — skipping",
                            sc.vendor,
                            sc.source_id,
                        )
                        continue
                    connector = connector_cls(sc)
                    key = (sc.tenant_id, sc.greenhouse_id)
                    scope_connectors.setdefault(key, []).append(connector)
            except json.JSONDecodeError as exc:
                logger.warning("LIVE_SOURCES_JSON parse error: %s — ignoring", exc)

        scopes = [
            cls._build_scope(hub, tenant_id, greenhouse_id, connectors, batcher)
            for (tenant_id, greenhouse_id), connectors in scope_connectors.items()
        ]
        logger.info("LiveRuntime: configured %d scope(s).", len(scopes))
        return cls(hub, scopes, batcher)

    @classmethod
    def _build_scope(
        cls,
        hub: LiveHub,
        tenant_id: str,
        greenhouse_id: str,
        connectors: list[LiveSourceConnector],
        batcher: Optional[PersistenceBatcher],
    ) -> _ScopeContext:
        """Build one _ScopeContext for a tenant/greenhouse pair."""
        source_configs = [c.config for c in connectors]

        # Derive per-scope timing from the actual source intervals FIRST,
        # because the scorer needs emit_interval for its forward-fill staleness
        # cutoff (LIVE_FORWARD_FILL_MAX_TICKS × emit_interval).
        #
        # emit_interval = min poll interval across sources (so the merger's
        # bucket width matches the fastest source in the scope).
        # grace_seconds = max(config default, max poll interval) so the slowest
        # source's data arrives before its bucket is flushed and dropped.
        # Fallback to MOCK_SCADA_INTERVAL_SECONDS when source_configs is empty.
        #
        # Design note on LIVE_FORWARD_FILL_MAX_TICKS basis: we use min(intervals)
        # (the fastest source cadence) rather than max(intervals) because the
        # staleness cutoff protects against a channel going *silent*, not against
        # it being slow-polled. A slow channel (e.g. 60s Hoogendoorn CO2) reports
        # once per its own interval — the merger forward-fills between those
        # reports. The cutoff fires only when a channel stops reporting entirely.
        # Using max(intervals) would allow a fast channel to go silent for up to
        # 5×60s=300s before scoring paused, which is far too long. Using
        # min(intervals) means scoring pauses after 5× the fastest cadence of
        # silence on ANY channel — a tighter, safer bound.
        if source_configs:
            intervals = [sc.poll_interval_seconds for sc in source_configs]
            emit_interval: float = min(intervals)
            grace_seconds: float = max(LIVE_MERGE_GRACE_SECONDS, max(intervals))
        else:
            emit_interval = MOCK_SCADA_INTERVAL_SECONDS
            grace_seconds = LIVE_MERGE_GRACE_SECONDS

        scorer: Any = None
        try:
            from src.live.scoring import LiveScorer  # lazy — may not exist yet
            scorer = LiveScorer(
                tenant_id=tenant_id,
                greenhouse_id=greenhouse_id,
                hub=hub,
                threshold=LIVE_ANOMALY_THRESHOLD,
                emit_interval=emit_interval,
            )
        except ImportError:
            logger.warning(
                "LiveScorer not available for %s/%s — running without anomaly scoring.",
                tenant_id,
                greenhouse_id,
            )

        async def on_reading(event: LiveReadingEvent) -> None:
            _attach_derived_vpd(event)
            scored = event
            if scorer is not None:
                try:
                    scored = await scorer.score(event)
                except Exception as exc:
                    logger.warning(
                        "Scorer error for %s/%s: %s — using unscored event",
                        tenant_id,
                        greenhouse_id,
                        exc,
                    )
            await hub.publish(tenant_id, greenhouse_id, "reading", scored)
            if batcher is not None:
                await batcher.add(scored)

        async def on_status(status_ev: SourceStatusEvent) -> None:
            await hub.publish(tenant_id, greenhouse_id, "source-status", status_ev)

        merger = ChannelMerger(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            emit_interval=emit_interval,
            grace_seconds=grace_seconds,
            source_configs=source_configs,
            on_reading=on_reading,
            on_status=on_status,
        )

        return _ScopeContext(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            connectors=connectors,
            merger=merger,
            scorer=scorer,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn all supervised tasks for connectors, mergers, and the batcher."""
        if self._batcher is not None:
            self._tasks.append(
                asyncio.create_task(
                    _supervised("persistence_flush", self._batcher.run),
                    name="persistence_flush",
                )
            )

        for scope in self._scopes:
            prefix = f"{scope.tenant_id}/{scope.greenhouse_id}"

            self._tasks.append(
                asyncio.create_task(
                    _supervised(f"{prefix}/merger", scope.merger.run),
                    name=f"merger_{scope.tenant_id}_{scope.greenhouse_id}",
                )
            )

            for connector in scope.connectors:
                label = f"{prefix}/{connector.config.source_id}"
                self._tasks.append(
                    asyncio.create_task(
                        _supervised(label, _run_connector, connector, scope.merger),
                        name=f"connector_{connector.config.source_id}",
                    )
                )

        logger.info("LiveRuntime: started %d task(s).", len(self._tasks))

    async def stop(self) -> None:
        """Cancel all tasks and release resources."""
        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Close connectors so their HTTP clients are released.
        for scope in self._scopes:
            for connector in scope.connectors:
                try:
                    await connector.close()
                except Exception as exc:
                    logger.warning(
                        "Connector close failed for %s: %s",
                        connector.config.source_id,
                        exc,
                    )

        for scope in self._scopes:
            if scope.scorer is not None:
                try:
                    await scope.scorer.close()
                except Exception as exc:
                    logger.warning("Scorer close failed for %s/%s: %s", scope.tenant_id, scope.greenhouse_id, exc)

        if self._batcher is not None:
            await self._batcher.close()

        logger.info("LiveRuntime stopped.")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def source_statuses(self, tenant_id: str, greenhouse_id: str) -> list[dict[str, str | None]]:
        """Return current health for each source in the given scope.

        Each entry contains ``source_id``, ``vendor``, ``status``, and
        ``last_seen`` (ISO-8601 string or ``None`` if no reading has arrived
        yet).  Returns ``[]`` when the scope is unknown (runtime not configured
        for that tenant/greenhouse pair).
        """
        merger = self._mergers.get((tenant_id, greenhouse_id))
        if merger is None:
            return []
        return merger.full_statuses()

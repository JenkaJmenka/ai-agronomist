"""Config-driven scope assembly for :class:`~src.live.runtime.LiveRuntime`.

Parses ``MOCK_SCADA_*`` and ``LIVE_SOURCES_JSON`` environment config into
per-scope contexts (connectors + merger + scorer).  This module is the
growth axis for new vendors and env knobs — the runtime itself only owns
task lifecycle.

The scorer (``src.live.scoring.LiveScorer``) is imported **lazily**:
if the module is absent (parallel agent not yet delivered), a passthrough
is used and a warning is logged — the rest of the pipeline still runs.
"""

from __future__ import annotations

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
from src.live.derived import attach_derived_channels
from src.live.events import LiveReadingEvent, SourceStatusEvent
from src.live.hub import LiveHub
from src.live.merge import ChannelMerger
from src.live.persistence import PersistenceBatcher
from src.live.sources import CONNECTOR_TYPES
from src.live.sources.base import LiveSourceConnector, SourceConfig

logger = logging.getLogger("live.runtime_builder")


@dataclass
class _ScopeContext:
    """All live components for a single tenant/greenhouse scope."""

    tenant_id: str
    greenhouse_id: str
    connectors: list[LiveSourceConnector]
    merger: ChannelMerger
    scorer: Any = field(default=None)  # LiveScorer | None


def build_scopes_from_config(
    hub: LiveHub,
) -> tuple[list[_ScopeContext], Optional[PersistenceBatcher]]:
    """Build all scope contexts (and the shared batcher) from env config.

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
        _build_scope(hub, tenant_id, greenhouse_id, connectors, batcher)
        for (tenant_id, greenhouse_id), connectors in scope_connectors.items()
    ]
    logger.info("LiveRuntime: configured %d scope(s).", len(scopes))
    return scopes, batcher


def _build_scope(
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
        attach_derived_channels(event)
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

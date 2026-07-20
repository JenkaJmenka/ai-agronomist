"""LiveRuntime — supervised orchestrator for live monitoring data sources.

Owns the task lifecycle for one :class:`~src.live.merge.ChannelMerger` per
scope (``tenant_id``/``greenhouse_id`` pair), wrapping each long-running
coroutine (merger loop, connector streaming, persistence flush) in a
restart-on-crash supervisor with exponential back-off
(:mod:`src.live.supervision`).  Config parsing and scope assembly live in
:mod:`src.live.runtime_builder`.

Usage
-----
::

    hub = get_live_hub()
    runtime = LiveRuntime.build_from_config(hub)
    await runtime.start()
    app.state.live_runtime = runtime
    ...
    await runtime.stop()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.live.hub import LiveHub
from src.live.merge import ChannelMerger
from src.live.persistence import PersistenceBatcher
from src.live.runtime_builder import _ScopeContext, build_scopes_from_config
from src.live.supervision import _run_connector, _supervised

logger = logging.getLogger("live.runtime")


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

        Thin delegate to
        :func:`src.live.runtime_builder.build_scopes_from_config`, which
        reads ``MOCK_SCADA_ENABLED``, ``MOCK_SCADA_SCOPES``, and
        ``LIVE_SOURCES_JSON``.  Unknown vendors in ``LIVE_SOURCES_JSON``
        are logged and skipped.
        """
        scopes, batcher = build_scopes_from_config(hub)
        return cls(hub, scopes, batcher)

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

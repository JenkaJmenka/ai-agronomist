"""LiveRuntime — supervised orchestrator for live monitoring data sources.

Owns the task lifecycle for one :class:`~src.live.merge.ChannelMerger` per
scope (``tenant_id``/``greenhouse_id`` pair), wrapping each long-running
coroutine (merger loop, connector streaming, persistence flush) in a
restart-on-crash supervisor with exponential back-off
(:mod:`src.live.supervision`).  Config parsing and scope assembly live in
:mod:`src.live.runtime_builder`.

Per-scope lifecycle
--------------------
Muting a scope (``suppress_persistence``) stops publish/persist side effects
but the merger + connector tasks keep polling and merging in the background.
That is fine for the "Clear all data" flow (a brief mute), but wrong for the
default idle state: an operator who never clicked "Go Live" should not have
mock/SCADA connectors running at all.  ``start_scope``/``stop_scope`` spawn
and cancel a scope's tasks outright, so an idle scope costs nothing and never
touches persistence.

Usage
-----
::

    hub = get_live_hub()
    runtime = LiveRuntime.build_from_config(hub)
    await runtime.start()
    app.state.live_runtime = runtime
    ...
    await runtime.start_scope(tenant_id, greenhouse_id)  # operator hits Go Live
    await runtime.stop_scope(tenant_id, greenhouse_id)   # operator turns Live off
    ...
    await runtime.stop()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.config import LIVE_AUTOSTART_ENABLED
from src.live.hub import LiveHub
from src.live.merge import ChannelMerger
from src.live.persistence import PersistenceBatcher
from src.live.runtime_builder import ScopeContext, build_scopes_from_config
from src.live.supervision import run_connector, supervised

logger = logging.getLogger("live.runtime")


class LiveRuntime:
    """Supervised orchestrator for all live monitoring data sources.

    Do not instantiate directly — use :meth:`build_from_config`.
    """

    def __init__(
        self,
        hub: LiveHub,
        scopes: list[ScopeContext],
        batcher: Optional[PersistenceBatcher],
    ) -> None:
        self._hub = hub
        self._scopes = scopes
        self._batcher = batcher
        self._batcher_task: Optional[asyncio.Task[None]] = None
        # Merger + connector tasks, keyed by scope. Empty entry (or missing
        # key) means the scope is idle — no connectors are running for it.
        self._scope_tasks: dict[tuple[str, str], list[asyncio.Task[None]]] = {}
        # Serializes start()/stop()/start_scope()/stop_scope() so a
        # double-clicked toggle can't spawn two task sets or cancel mid-spawn.
        self._lock = asyncio.Lock()
        # Index for fast scope lookup in source_statuses() and start_scope().
        self._mergers: dict[tuple[str, str], ChannelMerger] = {
            (sc.tenant_id, sc.greenhouse_id): sc.merger for sc in scopes
        }
        self._scopes_by_key: dict[tuple[str, str], ScopeContext] = {
            (sc.tenant_id, sc.greenhouse_id): sc for sc in scopes
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
        """Start the persistence batcher and, per config, per-scope tasks.

        The batcher always starts — it just flushes an empty buffer when
        nothing is streaming. When ``LIVE_AUTOSTART_ENABLED`` is False (the
        default), every configured scope starts idle: no merger or connector
        task is spawned for it, so mock/SCADA data cannot reach an uploaded
        CSV dataset until the operator hits Go Live. When True, every scope
        starts immediately (headless/demo deployments).
        """
        async with self._lock:
            if self._batcher is not None and self._batcher_task is None:
                self._batcher_task = asyncio.create_task(
                    supervised("persistence_flush", self._batcher.run),
                    name="persistence_flush",
                )

            if LIVE_AUTOSTART_ENABLED:
                for scope in self._scopes:
                    await self._start_scope_locked(scope)
            else:
                for scope in self._scopes:
                    self.suppress_persistence(scope.tenant_id, scope.greenhouse_id)
                if self._scopes:
                    logger.info(
                        "LiveRuntime: %d scope(s) idle pending Go Live.",
                        len(self._scopes),
                    )

        logger.info("LiveRuntime: started %d task(s).", len(self._all_tasks()))

    async def stop(self) -> None:
        """Cancel all tasks (batcher + every scope) and release resources.

        Works whether or not any scope was ever started.
        """
        async with self._lock:
            tasks = self._all_tasks()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._scope_tasks.clear()
            self._batcher_task = None

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

    async def start_scope(self, tenant_id: str, greenhouse_id: str) -> bool:
        """Start streaming + persistence for one scope (operator Go Live).

        Idempotent: returns False without side effects if the scope's tasks
        are already running, or if ``tenant_id``/``greenhouse_id`` is not a
        scope this runtime was configured for. Returns True once the merger
        and connector tasks are spawned and persistence is unmuted.
        """
        scope = self._scopes_by_key.get((tenant_id, greenhouse_id))
        if scope is None:
            logger.warning(
                "start_scope: unknown scope %s/%s — not configured", tenant_id, greenhouse_id
            )
            return False
        async with self._lock:
            return await self._start_scope_locked(scope)

    async def stop_scope(self, tenant_id: str, greenhouse_id: str) -> bool:
        """Stop streaming + persistence for one scope (operator turns Live off).

        Mutes persistence first so any event already past the cancellation
        point is dropped rather than published/persisted, then cancels the
        scope's tasks. Does **not** call ``connector.close()`` — connectors
        must stay restartable via a later ``start_scope`` call; ``close()``
        only runs as part of full runtime ``stop()`` teardown.

        Returns False if the scope had no running tasks (still mutes
        persistence in that case, so a pause before the first Go Live is a
        harmless no-op rather than an error).
        """
        async with self._lock:
            return await self._stop_scope_locked(tenant_id, greenhouse_id)

    async def _start_scope_locked(self, scope: ScopeContext) -> bool:
        """Spawn a scope's tasks. Caller must hold ``self._lock``."""
        key = (scope.tenant_id, scope.greenhouse_id)
        if self._scope_tasks.get(key):
            return False  # already running

        self.resume_persistence(scope.tenant_id, scope.greenhouse_id)

        prefix = f"{scope.tenant_id}/{scope.greenhouse_id}"
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(
                supervised(f"{prefix}/merger", scope.merger.run),
                name=f"merger_{scope.tenant_id}_{scope.greenhouse_id}",
            )
        ]
        for connector in scope.connectors:
            label = f"{prefix}/{connector.config.source_id}"
            tasks.append(
                asyncio.create_task(
                    supervised(label, run_connector, connector, scope.merger),
                    name=f"connector_{connector.config.source_id}",
                )
            )
        self._scope_tasks[key] = tasks
        logger.info("LiveRuntime: scope %s started (%d task(s)).", prefix, len(tasks))
        return True

    async def _stop_scope_locked(self, tenant_id: str, greenhouse_id: str) -> bool:
        """Cancel a scope's tasks. Caller must hold ``self._lock``."""
        key = (tenant_id, greenhouse_id)
        self.suppress_persistence(tenant_id, greenhouse_id)

        tasks = self._scope_tasks.pop(key, None)
        if not tasks:
            return False

        # supervised() restarts a coroutine even on a clean exit, so this
        # MUST be cancel() — letting stream() finish would just restart it.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("LiveRuntime: scope %s/%s stopped.", tenant_id, greenhouse_id)
        return True

    def _all_tasks(self) -> list[asyncio.Task[None]]:
        """All currently tracked tasks: the batcher plus every running scope."""
        tasks: list[asyncio.Task[None]] = []
        if self._batcher_task is not None:
            tasks.append(self._batcher_task)
        for scope_tasks in self._scope_tasks.values():
            tasks.extend(scope_tasks)
        return tasks

    def suppress_persistence(self, tenant_id: str, greenhouse_id: str) -> None:
        """Mute live publish + DB writes for a scope (used by clear-all-data)."""
        if self._batcher is not None:
            self._batcher.suppress_scope(tenant_id, greenhouse_id)
        else:
            from src.live.scope_control import suppress_live_scope

            suppress_live_scope(tenant_id, greenhouse_id)

    def resume_persistence(self, tenant_id: str, greenhouse_id: str) -> None:
        """Re-enable live publish + persistence (explicit Go Live only)."""
        if self._batcher is not None:
            self._batcher.resume_scope(tenant_id, greenhouse_id)
        else:
            from src.live.scope_control import resume_live_scope

            resume_live_scope(tenant_id, greenhouse_id)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def is_scope_active(self, tenant_id: str, greenhouse_id: str) -> bool:
        """Whether a scope's merger + connector tasks are currently running."""
        return bool(self._scope_tasks.get((tenant_id, greenhouse_id)))

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

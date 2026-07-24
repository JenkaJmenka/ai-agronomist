"""Shared live-scope gate used after Clear all data.

When a (tenant, greenhouse) scope is suppressed:
- PersistenceBatcher drops / ignores events for that scope
- Merger on_reading skips hub publish + scoring side effects

Resume only via an explicit API call (Go Live), never automatically on
SSE reconnect — otherwise a still-open live session immediately re-seeds
the DB the operator just wiped.

Process-local constraint
------------------------
``_suppressed_scopes`` is an **in-process set** — it lives in the memory of
the uvicorn worker that handled the suppress request.  Under a multi-worker
deployment, suppress/resume calls on one worker are **invisible** to other
workers: live events continue to be published and persisted by the other
processes.

Single-worker deployment is therefore a **documented invariant** of this
application.  The live monitoring docs (``docs/live_scada_connectors.md``
section 8) record this constraint and list future options (Redis leader lock,
``src/workers/`` pattern) for multi-worker scale-out.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("live.scope_control")

_suppressed_scopes: set[tuple[str, str]] = set()


def suppress_live_scope(tenant_id: str, greenhouse_id: str) -> None:
    """Mute live publish + DB persistence for a scope until resumed."""
    key = (tenant_id, greenhouse_id)
    _suppressed_scopes.add(key)
    logger.info("Live scope suppressed: %s/%s", tenant_id, greenhouse_id)


def resume_live_scope(tenant_id: str, greenhouse_id: str) -> None:
    """Allow live publish + persistence again for a scope."""
    key = (tenant_id, greenhouse_id)
    if key in _suppressed_scopes:
        _suppressed_scopes.discard(key)
        logger.info("Live scope resumed: %s/%s", tenant_id, greenhouse_id)


def is_live_scope_suppressed(tenant_id: str, greenhouse_id: str) -> bool:
    return (tenant_id, greenhouse_id) in _suppressed_scopes


def clear_all_suppressed_scopes() -> None:
    """Test helper — reset gate state between cases."""
    _suppressed_scopes.clear()

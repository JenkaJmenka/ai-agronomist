"""Shared live-scope gate used after Clear all data.

When a (tenant, greenhouse) scope is suppressed:
- PersistenceBatcher drops / ignores events for that scope
- Merger on_reading skips hub publish + scoring side effects

Resume only via an explicit API call (Go Live), never automatically on
SSE reconnect — otherwise a still-open live session immediately re-seeds
the DB the operator just wiped.
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

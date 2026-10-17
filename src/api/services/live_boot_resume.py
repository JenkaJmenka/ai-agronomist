"""Restore operator-chosen live scopes when the API process boots.

Why this exists
---------------
Commit ``9c14dc3`` made an idle scope stop its connectors entirely rather than
merely mute persistence — correct, because an operator who never pressed Go
Live should not have mock/SCADA data reaching their uploaded dataset.  But the
*only* record that Go Live had been pressed lived in ``LiveRuntime``'s
in-process task map, so every Railway redeploy returned every scope to idle
with no resume and no signal.

This module closes that loop by replaying the persisted desired state
(:mod:`src.api.services.live_scope_state_service`, migration 028) into
``LiveRuntime.start_scope``.

What it deliberately does NOT do
--------------------------------
``LIVE_AUTOSTART_ENABLED=false`` semantics survive intact: this only starts a
scope that an operator explicitly turned on and that nothing has turned off
since.  A scope with no stored row stays idle.  A scope stored ``live`` but no
longer present in ``MOCK_SCADA_SCOPES`` / ``LIVE_SOURCES_JSON`` is *not*
started — ``start_scope`` would reject it anyway, but filtering first lets the
boot log name it, which is the difference between a silent feed and a
diagnosable one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from src.api.services.live_scope_state_service import fetch_live_scopes
from src.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

Scope = tuple[str, str]

# Rendered in the boot log in place of an empty scope list, so an operator
# reading the line can tell "none" apart from a truncated message.
_NO_SCOPES = "(none)"


@dataclass(frozen=True)
class BootResumeSummary:
    """Outcome of one boot-resume pass, for logging and for tests."""

    resumed: tuple[Scope, ...] = ()
    idle: tuple[Scope, ...] = ()
    stored_live_not_configured: tuple[Scope, ...] = ()
    failed: tuple[Scope, ...] = field(default=())


def _render(scopes: tuple[Scope, ...]) -> str:
    """Format scopes as ``tenant/greenhouse`` for one log line."""
    if not scopes:
        return _NO_SCOPES
    return ", ".join(f"{tenant}/{greenhouse}" for tenant, greenhouse in scopes)


def log_boot_resume(summary: BootResumeSummary) -> None:
    """Emit the startup line that replaced the old ``Live runtime started.``.

    The old line said nothing about which scopes were actually streaming, so a
    deployment that silently dropped every feed looked identical to a healthy
    one in the logs.  This names all four buckets every boot.
    """
    logger.info(
        "Live runtime started — resumed: %s | idle: %s | "
        "stored live but not configured: %s | failed to resume: %s",
        _render(summary.resumed),
        _render(summary.idle),
        _render(summary.stored_live_not_configured),
        _render(summary.failed),
    )
    if summary.stored_live_not_configured:
        logger.warning(
            "Live scope(s) %s are stored live but have no source configured "
            "in MOCK_SCADA_SCOPES / LIVE_SOURCES_JSON — not resumed. Restore "
            "the env entry or pause them to clear this warning.",
            _render(summary.stored_live_not_configured),
        )


async def resume_persisted_live_scopes(runtime: Any) -> BootResumeSummary:
    """Start every configured scope whose stored desired state is ``live``.

    Never raises: a scope that fails to start is recorded in
    ``summary.failed`` and the remaining scopes are still attempted, because
    one wedged connector must not keep the others dark.
    """
    async with AsyncSessionLocal() as db:
        stored_live = set(await fetch_live_scopes(db))

    configured = list(runtime.configured_scopes())
    configured_set = set(configured)

    to_resume = [scope for scope in configured if scope in stored_live]
    idle = tuple(scope for scope in configured if scope not in stored_live)
    orphaned = tuple(sorted(stored_live - configured_set))

    resumed: list[Scope] = []
    failed: list[Scope] = []
    for tenant_id, greenhouse_id in to_resume:
        try:
            await runtime.start_scope(tenant_id, greenhouse_id)
        except Exception as exc:  # noqa: BLE001 — one bad scope must not dark the rest
            logger.error(
                "Boot resume failed for live scope %s/%s: %s",
                tenant_id,
                greenhouse_id,
                exc,
                exc_info=True,
            )
            failed.append((tenant_id, greenhouse_id))
        else:
            resumed.append((tenant_id, greenhouse_id))

    return BootResumeSummary(
        resumed=tuple(resumed),
        idle=idle,
        stored_live_not_configured=orphaned,
        failed=tuple(failed),
    )

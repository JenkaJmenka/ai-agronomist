"""Live monitoring SSE endpoint (Phase 2B).

Streams Redis-backed live sensor events to connected browsers/clients.
Each connection subscribes via :class:`~src.live.hub.LiveHub`, which
replays missed events and then tails the live stream.

Route contract
--------------
GET /api/live/events?tenant_id=...&greenhouse_id=default
  - Auth: X-API-Key header (same pattern as feedback / incidents routes).
  - Resume: Last-Event-ID request header (SSE protocol) or ?last_event_id=
    query param (curl testing).
  - Response: text/event-stream; hard-deadline close after
    LIVE_MAX_STREAM_SECONDS (monotonic clock); browser auto-reconnects.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.services import readings_service
from src.api.services.live_freshness import build_scope_freshness
from src.api.services.live_hub_provider import LiveHub, get_live_hub
from src.api.services.live_scope_state_service import (
    DesiredScopeState,
    store_desired_state,
)
from src.config import LIVE_HEARTBEAT_SECONDS, LIVE_MAX_STREAM_SECONDS
from src.db.session import get_db

router = APIRouter(prefix="/api/live", tags=["Live"])

# Discriminated values for the greenhouse_status field in /api/live/status.
# These constants let callers pattern-match on a well-known vocabulary instead
# of inferring state from the empty-sources list (the previous silent failure).
_GH_STATUS_NOT_REGISTERED = "not_registered"
_GH_STATUS_NO_SOURCE = "no_source"
_GH_STATUS_LIVE = "live"
_GH_STATUS_PAUSED = "paused"

_SSE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
    # Disable Nginx/proxy response buffering so frames flush immediately.
    "X-Accel-Buffering": "no",
}


def _heartbeat_frame() -> str:
    """Build a heartbeat SSE frame with the current UTC timestamp."""
    ts = datetime.now(timezone.utc).isoformat()
    return f'event: heartbeat\ndata: {{"ts":"{ts}"}}\n\n'


async def _stream_events(
    hub: LiveHub,
    tenant_id: str,
    greenhouse_id: str,
    last_event_id: Optional[str],
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted frames until the deadline or client disconnect.

    The deadline check runs **after** emitting each frame so that replay
    events and the first idle heartbeat are always delivered even when the
    stream opens close to the deadline (relevant in tests with a tiny
    LIVE_MAX_STREAM_SECONDS override).
    """
    deadline = time.monotonic() + LIVE_MAX_STREAM_SECONDS
    last_heartbeat_at: float = 0.0

    async for entry_id, event_type, data_json in hub.subscribe(
        tenant_id, greenhouse_id, last_event_id=last_event_id
    ):
        if event_type == "idle":
            now = time.monotonic()
            if now - last_heartbeat_at >= LIVE_HEARTBEAT_SECONDS:
                last_heartbeat_at = now
                yield _heartbeat_frame()
            else:
                yield ": keep-alive\n\n"
        elif event_type == "reset":
            yield f"event: reset\ndata: {data_json}\n\n"
        else:
            # Normal event: id + named event + data.
            yield f"id: {entry_id}\nevent: {event_type}\ndata: {data_json}\n\n"

        # Deadline check runs after emitting so each iteration is always
        # flushed.  In production the deadline is 3600 s; in tests it can be
        # patched to microseconds to get a finite response body.
        if time.monotonic() >= deadline:
            break


@router.get("/status")
async def live_source_status(
    request: Request,
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    auth_tenant: Optional[str] = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Return current health + freshness of all live data sources for a scope.

    Response shape (always HTTP 200):
    ::

        {
          "greenhouse_status": "not_registered" | "no_source" | "live" | "paused",
          "sources": [...],
          "freshness": {
            "state": "live" | "paused" | "not_configured",
            "newest_reading_at": "2026-10-16T09:00:00+00:00" | null,
            "age_seconds": 42.0 | null,
            "is_stale": false,
            "threshold_seconds": 180.0
          }
        }

    ``freshness`` lives here rather than on a new endpoint because this route
    is already the one place a caller asks "what is this scope doing", it
    already resolves the runtime and a DB session, and a staleness badge
    renders next to the status it qualifies — a second endpoint would make
    the UI do two round-trips to draw one indicator. ``sources`` and
    ``greenhouse_status`` are unchanged, so existing consumers keep working.

    ``freshness.state`` is deliberately narrower than ``greenhouse_status``:
    it describes the connectors only (live / paused / not_configured) and
    never the registry states, so a caller can reason about liveness without
    first excluding ``not_registered``. ``is_stale`` is true only for a
    ``live`` scope — including a live scope that has never written a row at
    all. See :mod:`src.api.services.live_freshness` for the age/clock rules
    (ages are not clamped: future-dated readings give a negative age).

    The ``greenhouse_status`` discriminator lets callers tell apart the
    empty-source states that previously all returned ``{"sources": []}``
    and were indistinguishable, plus a configured-but-idle state:

    * ``not_registered`` — no greenhouse record in the registry or any
      readings table; the tenant needs to be provisioned.
    * ``no_source``      — greenhouse exists but the live runtime is not
      watching it; use POST /api/readings/greenhouses to register and
      configure LIVE_SOURCES_JSON / MOCK_SCADA_ENABLED for this scope.
    * ``paused``         — the scope is configured but its connectors are
      not running (idle pending Go Live, or explicitly paused).
    * ``live``            — the runtime has an active scope for this
      greenhouse (sources may still have ``last_seen: null`` while the
      first ticks arrive).

    The ``sources`` list is always present so existing consumers that only
    read ``sources`` continue to work without changes.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    runtime = getattr(request.app.state, "live_runtime", None)

    sources: list[dict[str, str | None]] = []
    if runtime is not None:
        sources = runtime.source_statuses(tenant_id, greenhouse_id)

    if sources:
        # Runtime has a configured scope for this greenhouse. Distinguish
        # "actively streaming" from "configured but idle" (pre-Go Live or
        # explicitly paused) when we can determine that — runtime is None
        # in the readings-service fallback mode, where we can't tell, so
        # default to the pre-existing "live" behavior.
        if runtime is not None and not runtime.is_scope_active(tenant_id, greenhouse_id):
            greenhouse_status = _GH_STATUS_PAUSED
        else:
            greenhouse_status = _GH_STATUS_LIVE
    else:
        # Distinguish "greenhouse unknown" from "greenhouse known but no source".
        exists = await readings_service.greenhouse_exists(
            db, tenant_id=tenant_id, greenhouse_id=greenhouse_id
        )
        greenhouse_status = (
            _GH_STATUS_NO_SOURCE if exists else _GH_STATUS_NOT_REGISTERED
        )

    freshness = await build_scope_freshness(
        db, runtime=runtime, tenant_id=tenant_id, greenhouse_id=greenhouse_id
    )

    return JSONResponse(
        content={
            "greenhouse_status": greenhouse_status,
            "sources": sources,
            "freshness": freshness.to_dict(),
        }
    )


@router.post("/resume")
async def resume_live_scope_endpoint(
    request: Request,
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    auth_tenant: Optional[str] = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Explicitly start the scope's connectors and re-enable persistence.

    Called when the operator turns Go Live on — not on every SSE reconnect,
    which would immediately re-seed the wiped database. Spawns the merger
    and connector tasks for this scope (idempotent) so idle scopes don't
    stream until this is called.

    ``resumed`` means exactly one thing: live data will flow for this scope
    from now on. ``scope_status`` explains why:

    * ``started``         — the scope was idle; its tasks were just spawned.
    * ``already_running`` — the scope was already streaming (idempotent
      no-op call) — a success, not a failure.
    * ``not_configured``  — this tenant/greenhouse has no source wired up in
      ``MOCK_SCADA_SCOPES`` / ``LIVE_SOURCES_JSON``, so no data will ever
      flow; ``resumed`` is False here so a green Go Live toggle doesn't lie
      to the operator.

    Except in the ``not_configured`` case, the choice is recorded in
    ``live_scope_state`` so the API resumes this scope after a restart.
    ``not_configured`` is excluded on purpose: storing ``live`` for a scope
    with no source would leave a row that can only ever produce a boot
    warning. ``desired_state_persisted`` reports whether that write
    succeeded — it is False on a deploy where migration 028 has not run, and
    the scope then streams until the next restart only.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    runtime = getattr(request.app.state, "live_runtime", None)
    if runtime is not None:
        if not runtime.is_scope_configured(tenant_id, greenhouse_id):
            return {
                "resumed": False,
                "scope_status": "not_configured",
                "tenant_id": tenant_id,
                "greenhouse_id": greenhouse_id,
            }
        started = await runtime.start_scope(tenant_id, greenhouse_id)
        persisted = await store_desired_state(
            db,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            desired_state=DesiredScopeState.LIVE,
        )
        return {
            "resumed": True,
            "scope_status": "started" if started else "already_running",
            "desired_state_persisted": persisted,
            "tenant_id": tenant_id,
            "greenhouse_id": greenhouse_id,
        }

    # No runtime on app.state (live sources disabled for this deployment).
    # scope_control only tracks a suppression flag — it has no notion of
    # which scopes are configured — so we can't honestly report "started"
    # vs "already_running" vs "not_configured" here without fabricating an
    # answer. "unmanaged" says so plainly rather than guessing.
    from src.live.scope_control import resume_live_scope

    resume_live_scope(tenant_id, greenhouse_id)
    persisted = await store_desired_state(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        desired_state=DesiredScopeState.LIVE,
    )
    return {
        "resumed": True,
        "scope_status": "unmanaged",
        "desired_state_persisted": persisted,
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
    }


@router.post("/pause")
async def pause_live_scope_endpoint(
    request: Request,
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    auth_tenant: Optional[str] = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Explicitly stop the scope's connectors when the operator turns Go Live off.

    Mutes persistence and cancels the merger + connector tasks for this
    scope so historical windows show only uploaded data and no mock/SCADA
    reading is generated at all — mirrors /resume for the opposite
    transition.

    Unlike /resume, ``paused`` is always True: nothing streams for this
    scope once the call returns, whether it was running, already idle, or
    never configured — there's no "wrong" outcome to flag. ``scope_status``
    records which case it was, for diagnostics only:

    * ``stopped``        — the scope was running; its tasks were cancelled.
    * ``not_running``    — the scope was already idle (idempotent no-op).
    * ``not_configured`` — this tenant/greenhouse has no source wired up;
      there was nothing to stop.

    Unlike /resume, ``paused`` is recorded in ``live_scope_state`` in **every**
    case including ``not_configured``: pause is the safe direction, and an
    operator who turns a scope off must not find it streaming again after the
    next redeploy. ``desired_state_persisted`` reports whether that write
    succeeded.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    runtime = getattr(request.app.state, "live_runtime", None)
    persisted = await store_desired_state(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        desired_state=DesiredScopeState.PAUSED,
    )
    if runtime is not None:
        if not runtime.is_scope_configured(tenant_id, greenhouse_id):
            return {
                "paused": True,
                "scope_status": "not_configured",
                "desired_state_persisted": persisted,
                "tenant_id": tenant_id,
                "greenhouse_id": greenhouse_id,
            }
        stopped = await runtime.stop_scope(tenant_id, greenhouse_id)
        return {
            "paused": True,
            "scope_status": "stopped" if stopped else "not_running",
            "desired_state_persisted": persisted,
            "tenant_id": tenant_id,
            "greenhouse_id": greenhouse_id,
        }

    # See /resume above for why "unmanaged" rather than guessing.
    from src.live.scope_control import suppress_live_scope

    suppress_live_scope(tenant_id, greenhouse_id)
    return {
        "paused": True,
        "scope_status": "unmanaged",
        "desired_state_persisted": persisted,
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
    }


@router.get("/events")
async def stream_live_events(
    request: Request,
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    last_event_id: Optional[str] = Query(default=None),
    auth_tenant: Optional[str] = Depends(require_api_key),
    hub: LiveHub = Depends(get_live_hub),
) -> StreamingResponse:
    """SSE live-monitoring stream (Phase 2B).

    Replays missed events when Last-Event-ID / last_event_id is supplied,
    then tails the Redis stream live.  Hard-closes after
    ``LIVE_MAX_STREAM_SECONDS`` so the browser reconnects and the server
    sheds stale connections automatically.

    Does **not** resume a scope muted by Clear all data — use POST /resume
    (wired to Go Live) for that.
    """
    enforce_tenant_access(auth_tenant, tenant_id)

    # SSE protocol: Last-Event-ID header takes precedence over query param.
    resume_id: Optional[str] = request.headers.get("Last-Event-ID") or last_event_id

    return StreamingResponse(
        _stream_events(hub, tenant_id, greenhouse_id, resume_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

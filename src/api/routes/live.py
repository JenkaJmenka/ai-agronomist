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
from src.api.services.live_hub_provider import LiveHub, get_live_hub
from src.config import LIVE_HEARTBEAT_SECONDS, LIVE_MAX_STREAM_SECONDS
from src.db.session import get_db

router = APIRouter(prefix="/api/live", tags=["Live"])

# Discriminated values for the greenhouse_status field in /api/live/status.
# These constants let callers pattern-match on a well-known vocabulary instead
# of inferring state from the empty-sources list (the previous silent failure).
_GH_STATUS_NOT_REGISTERED = "not_registered"
_GH_STATUS_NO_SOURCE = "no_source"
_GH_STATUS_LIVE = "live"

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
    """Return current health status of all live data sources for a scope.

    Response shape (always HTTP 200):
    ::

        {
          "greenhouse_status": "not_registered" | "no_source" | "live",
          "sources": [...]
        }

    The ``greenhouse_status`` discriminator lets callers tell apart three
    empty-source states that previously all returned ``{"sources": []}``
    and were indistinguishable:

    * ``not_registered`` — no greenhouse record in the registry or any
      readings table; the tenant needs to be provisioned.
    * ``no_source``      — greenhouse exists but the live runtime is not
      watching it; use POST /api/readings/greenhouses to register and
      configure LIVE_SOURCES_JSON / MOCK_SCADA_ENABLED for this scope.
    * ``live``           — the runtime has an active scope for this
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
        # Runtime has an active scope → greenhouse is live (data may not have
        # arrived yet; last_seen: null is the expected "connected, warming up"
        # state for a freshly started scope).
        greenhouse_status = _GH_STATUS_LIVE
    else:
        # Distinguish "greenhouse unknown" from "greenhouse known but no source".
        exists = await readings_service.greenhouse_exists(
            db, tenant_id=tenant_id, greenhouse_id=greenhouse_id
        )
        greenhouse_status = (
            _GH_STATUS_NO_SOURCE if exists else _GH_STATUS_NOT_REGISTERED
        )

    return JSONResponse(
        content={"greenhouse_status": greenhouse_status, "sources": sources}
    )


@router.post("/resume")
async def resume_live_scope_endpoint(
    request: Request,
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query(default="default"),
    auth_tenant: Optional[str] = Depends(require_api_key),
) -> dict[str, Any]:
    """Explicitly re-enable live publish/persistence after Clear all data.

    Called when the operator turns Go Live on — not on every SSE reconnect,
    which would immediately re-seed the wiped database.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    runtime = getattr(request.app.state, "live_runtime", None)
    if runtime is not None:
        runtime.resume_persistence(tenant_id, greenhouse_id)
        return {"resumed": True, "tenant_id": tenant_id, "greenhouse_id": greenhouse_id}
    from src.live.scope_control import resume_live_scope

    resume_live_scope(tenant_id, greenhouse_id)
    return {"resumed": True, "tenant_id": tenant_id, "greenhouse_id": greenhouse_id}


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

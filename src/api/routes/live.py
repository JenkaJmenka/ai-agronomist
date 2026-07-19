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

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.services.live_hub_provider import LiveHub, get_live_hub
from src.config import LIVE_HEARTBEAT_SECONDS, LIVE_MAX_STREAM_SECONDS

router = APIRouter(prefix="/api/live", tags=["Live"])

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
) -> Any:
    """Return current health status of all live data sources for a scope.

    Mirrors the auth pattern of the events route.  When the live runtime
    is absent or disabled, returns ``{"sources": []}`` with HTTP 200 so
    callers can always consume the response without special-casing 503.
    """
    enforce_tenant_access(auth_tenant, tenant_id)
    runtime = getattr(request.app.state, "live_runtime", None)
    if runtime is None:
        return JSONResponse(content={"sources": []})
    return {"sources": runtime.source_statuses(tenant_id, greenhouse_id)}


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
    """
    enforce_tenant_access(auth_tenant, tenant_id)

    # SSE protocol: Last-Event-ID header takes precedence over query param.
    resume_id: Optional[str] = request.headers.get("Last-Event-ID") or last_event_id

    return StreamingResponse(
        _stream_events(hub, tenant_id, greenhouse_id, resume_id),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

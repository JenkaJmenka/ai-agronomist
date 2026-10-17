"""Response schemas for the live monitoring routes.

Why this module exists
----------------------
``GET /api/live/status`` returned an ad-hoc dict through ``JSONResponse`` and
declared no ``response_model``, so FastAPI emitted the 200 body as a bare
``{}`` schema and ``openapi-typescript`` rendered it as ``unknown``.  The
``freshness`` contract was therefore absent from ``web/openapi.json`` and the
``openapi-drift`` CI job could not protect it — the frontend had to hand-write
a Zod schema to get any typing at all.  These models put the whole response
into the schema so the drift check covers it.

This is a **typing** change, not a contract change: every field below is typed
to match the bytes the route already emitted.

Timestamps are ``str``, deliberately
------------------------------------
``last_seen`` and ``newest_reading_at`` are produced by calling
``datetime.isoformat()`` upstream (``src/live/merge.py`` and
``ScopeFreshness.to_dict``), which renders UTC as ``...+00:00``.  Typing them
as ``datetime`` would hand serialization to Pydantic, which renders the same
instant as ``...Z`` — a wire-shape change for every existing consumer.  They
stay ``str`` so the bytes are unchanged.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# --- greenhouse_status vocabulary -------------------------------------------
# Discriminated values for the greenhouse_status field in /api/live/status.
# These constants let callers pattern-match on a well-known vocabulary instead
# of inferring state from the empty-sources list (the previous silent failure).
GH_STATUS_NOT_REGISTERED = "not_registered"
GH_STATUS_NO_SOURCE = "no_source"
GH_STATUS_LIVE = "live"
GH_STATUS_PAUSED = "paused"

GreenhouseStatus = Literal["not_registered", "no_source", "live", "paused"]

# --- freshness.state vocabulary ---------------------------------------------
# About the *connectors*, and intentionally narrower than the sibling
# ``greenhouse_status`` field, which also covers registry states
# (``not_registered`` / ``no_source``) that say nothing about liveness.
SCOPE_STATE_LIVE = "live"
SCOPE_STATE_PAUSED = "paused"
SCOPE_STATE_NOT_CONFIGURED = "not_configured"

FreshnessState = Literal["live", "paused", "not_configured"]


class LiveSourceStatus(BaseModel):
    """Health of one data source within a scope.

    Mirrors ``MergeBuffer.full_statuses`` entries. The ``status`` vocabulary is
    the same closed set already pinned by ``SourceStatusEvent`` in
    ``src/live/events.py`` — that module is the only producer of these values.
    """

    source_id: str
    vendor: str
    status: Literal["online", "degraded", "offline"]
    # Required-but-nullable: the producer always emits the key, so the schema
    # says so. Marking it optional would understate the contract.
    last_seen: Optional[str] = Field(
        ...,
        description="ISO-8601 timestamp of the last reading, or null if never seen.",
    )


class LiveFreshness(BaseModel):
    """Is data actually arriving for this scope — distinct from "is Go Live on".

    See :mod:`src.api.services.live_freshness` for the age/clock rules.
    """

    state: FreshnessState
    newest_reading_at: Optional[str] = Field(
        ...,
        description=(
            "ISO-8601 timestamp of the newest persisted row across both reading "
            "tables, or null when the scope has never written one."
        ),
    )
    # No ``ge`` constraint on purpose — see the description.
    age_seconds: Optional[float] = Field(
        ...,
        description=(
            "now - newest_reading_at, in seconds. Null when there are no rows. "
            "Deliberately NOT clamped to zero and therefore signed: readings in "
            "this system are routinely future-dated, so a negative age is a "
            "normal value meaning 'the newest row is in the future'. A negative "
            "age is never stale."
        ),
    )
    is_stale: bool = Field(
        ...,
        description=(
            "True only for a 'live' scope — including a live scope that has "
            "never written a row at all. A paused or unconfigured scope is off, "
            "not broken."
        ),
    )
    threshold_seconds: float = Field(
        ...,
        description="Age above which a live scope is considered stale.",
    )


class LiveStatusResponse(BaseModel):
    """Body of ``GET /api/live/status`` (always HTTP 200)."""

    greenhouse_status: GreenhouseStatus
    sources: List[LiveSourceStatus] = Field(
        ...,
        description=(
            "Always present, so consumers that only read 'sources' keep working."
        ),
    )
    freshness: LiveFreshness

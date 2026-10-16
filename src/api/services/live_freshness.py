"""Per-scope live-feed freshness: is this feed actually writing rows?

``GET /api/live/status`` already answered "is a scope configured and are its
connectors running".  It could not answer the question that actually matters
to an operator — *is data arriving* — and a production audit found zero
``source='live'`` rows for any tenant while the UI happily reported a
configured scope.  "Go Live is on" and "the feed works" are different claims
and this module computes the second one.

Signal
------
The newest persisted row timestamp for the scope, taken as the max across
**both** tables the live writer targets (:mod:`src.live.persistence` writes
``sensor_readings`` and ``raw_readings`` in one transaction).  Both are
checked because they do not always both receive a row: a bucket missing any
of the four canonical channels is skipped for ``sensor_readings`` but still
written to ``raw_readings``, so reading only the canonical table would call a
partially-reporting-but-healthy feed stale.

The source column is deliberately **not** filtered on.  Freshness asks "has
anything landed for this greenhouse recently", and a scope whose rows arrive
from a CSV upload instead of the connector is not a stopped feed.

Clock
-----
Age is ``now - newest`` and is **never clamped to zero**.  Readings in this
repo are routinely future-dated relative to wall clock (the tenant_1 CSV runs
to 2026-12-31), so a negative age is a normal, meaningful value meaning "the
newest row is in the future".  Clamping it would erase that distinction and
make a future-dated dataset indistinguishable from one written this second.
A negative age is never stale.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.csv_persist import normalize_persist_timestamp
from src.config import LIVE_STALE_AFTER_SECONDS
from src.db.models import RawReading, SensorReading

# Scope states reported by the freshness block. These are about the
# *connectors*, and are intentionally narrower than the sibling
# ``greenhouse_status`` field, which also covers registry states
# (``not_registered`` / ``no_source``) that say nothing about liveness.
SCOPE_STATE_LIVE = "live"
SCOPE_STATE_PAUSED = "paused"
SCOPE_STATE_NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class ScopeFreshness:
    """Freshness verdict for one (tenant, greenhouse) live scope."""

    state: str
    newest_reading_at: Optional[datetime.datetime]
    age_seconds: Optional[float]
    is_stale: bool
    threshold_seconds: float

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection for the API response."""
        return {
            "state": self.state,
            "newest_reading_at": (
                self.newest_reading_at.isoformat()
                if self.newest_reading_at is not None
                else None
            ),
            "age_seconds": self.age_seconds,
            "is_stale": self.is_stale,
            "threshold_seconds": self.threshold_seconds,
        }


def resolve_scope_state(
    runtime: Any,
    tenant_id: str,
    greenhouse_id: str,
) -> str:
    """Classify a scope as live / paused / not_configured.

    ``runtime`` may be ``None`` (live sources disabled for this deployment),
    in which case no scope is configured.
    """
    if runtime is None or not runtime.is_scope_configured(tenant_id, greenhouse_id):
        return SCOPE_STATE_NOT_CONFIGURED
    if runtime.is_scope_active(tenant_id, greenhouse_id):
        return SCOPE_STATE_LIVE
    return SCOPE_STATE_PAUSED


async def fetch_newest_reading_at(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
) -> Optional[datetime.datetime]:
    """Newest row timestamp for the scope across both reading tables.

    Returns ``None`` when the scope has no rows in either table.
    """
    candidates: list[datetime.datetime] = []
    for model in (SensorReading, RawReading):
        stmt = select(func.max(model.timestamp)).where(
            model.tenant_id == tenant_id,
            model.greenhouse_id == greenhouse_id,
        )
        newest = (await db.execute(stmt)).scalar_one_or_none()
        if newest is not None:
            candidates.append(normalize_persist_timestamp(newest))
    if not candidates:
        return None
    return max(candidates)


def _is_stale(state: str, age_seconds: Optional[float]) -> bool:
    """Whether a scope counts as stale. Only a *live* scope can be stale.

    A paused or unconfigured scope is off, not broken — flagging it would
    make the signal useless, because most scopes are idle most of the time.
    A live scope with no rows at all (``age_seconds is None``) IS stale: that
    is the exact production state this signal was added to surface.
    """
    if state != SCOPE_STATE_LIVE:
        return False
    if age_seconds is None:
        return True
    return age_seconds > LIVE_STALE_AFTER_SECONDS


async def build_scope_freshness(
    db: AsyncSession,
    *,
    runtime: Any,
    tenant_id: str,
    greenhouse_id: str,
    now: Optional[datetime.datetime] = None,
) -> ScopeFreshness:
    """Assemble the freshness block for one scope.

    ``now`` is injectable so tests can pin the clock instead of sleeping.
    """
    reference = now or datetime.datetime.now(datetime.timezone.utc)
    state = resolve_scope_state(runtime, tenant_id, greenhouse_id)
    newest = await fetch_newest_reading_at(
        db, tenant_id=tenant_id, greenhouse_id=greenhouse_id
    )
    age_seconds = (
        (normalize_persist_timestamp(reference) - newest).total_seconds()
        if newest is not None
        else None
    )
    return ScopeFreshness(
        state=state,
        newest_reading_at=newest,
        age_seconds=age_seconds,
        is_stale=_is_stale(state, age_seconds),
        threshold_seconds=LIVE_STALE_AFTER_SECONDS,
    )

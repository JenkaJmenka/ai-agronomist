"""Read/write the persisted Go Live desired state for live scopes.

Backs the ``live_scope_state`` table (:class:`~src.db.models.LiveScopeState`,
migration 028).  Three callers:

* ``POST /api/live/resume`` writes ``live``.
* ``POST /api/live/pause`` and both tenant data wipes write ``paused``.
* API boot (:mod:`src.api.services.live_boot_resume`) reads the ``live`` set
  and restarts those scopes.

Missing-table tolerance
-----------------------
Only the **API** service runs ``alembic upgrade head``
(``docker/start-api.sh``); the worker does not.  A failed API deploy therefore
leaves newer code running against an older schema, and this table may simply
not exist.  Every function here is written so that a missing table degrades to
the pre-028 behaviour (nothing persisted, nothing resumed) instead of taking
the process down:

* :func:`fetch_live_scopes` returns ``[]`` and logs a warning.
* :func:`store_desired_state` returns ``False`` and logs a warning, so the
  caller can still perform the in-process runtime action and report honestly
  that the choice was not durably recorded.

This is deliberately *not* a bare ``except: pass`` — both paths log the
tenant/greenhouse and the exception, and both surface the failure in their
return value.
"""

from __future__ import annotations

import datetime
import logging
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import LiveScopeState
from src.db.upsert import bulk_upsert_rows

logger = logging.getLogger(__name__)

# Conflict target matching LiveScopeState's composite primary key.
_SCOPE_CONFLICT_COLS: tuple[str, ...] = ("tenant_id", "greenhouse_id")


class DesiredScopeState(StrEnum):
    """Operator intent for a live scope, as stored in ``desired_state``.

    Values match the ``ck_live_scope_state_desired_state`` check constraint.
    """

    LIVE = "live"
    PAUSED = "paused"


async def store_desired_state(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    desired_state: DesiredScopeState,
) -> bool:
    """Upsert one scope's desired state. Returns whether it was persisted.

    Does **not** commit — the caller owns the transaction, so a wipe can
    record ``paused`` atomically with the deletes it is performing.
    """
    row = {
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
        "desired_state": desired_state.value,
        # Passed explicitly: the model's ``onupdate`` does not apply to an
        # ON CONFLICT DO UPDATE set clause, so without this the timestamp
        # would keep the value written by the very first insert.
        "updated_at": datetime.datetime.now(datetime.timezone.utc),
    }
    try:
        await bulk_upsert_rows(
            db,
            LiveScopeState,
            [row],
            update_cols=("desired_state", "updated_at"),
            conflict_cols=_SCOPE_CONFLICT_COLS,
        )
    except SQLAlchemyError as exc:
        logger.warning(
            "Could not persist desired live state %s for %s/%s — the scope "
            "will not survive a restart (is migration 028 applied?): %s",
            desired_state.value,
            tenant_id,
            greenhouse_id,
            exc,
        )
        await db.rollback()
        return False
    return True


async def fetch_live_scopes(db: AsyncSession) -> list[tuple[str, str]]:
    """Return every ``(tenant_id, greenhouse_id)`` whose desired state is live.

    Returns ``[]`` when the table is absent or unreadable, which makes boot
    resume a no-op rather than a crash.
    """
    stmt = select(LiveScopeState.tenant_id, LiveScopeState.greenhouse_id).where(
        LiveScopeState.desired_state == DesiredScopeState.LIVE.value
    )
    try:
        result = await db.execute(stmt)
    except SQLAlchemyError as exc:
        logger.warning(
            "Could not read live_scope_state — no scope will be resumed on "
            "this boot (is migration 028 applied?): %s",
            exc,
        )
        return []
    return [(row.tenant_id, row.greenhouse_id) for row in result]


async def fetch_desired_state(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
) -> DesiredScopeState | None:
    """Return one scope's stored desired state, or ``None`` if never set."""
    stmt = select(LiveScopeState.desired_state).where(
        LiveScopeState.tenant_id == tenant_id,
        LiveScopeState.greenhouse_id == greenhouse_id,
    )
    try:
        stored = (await db.execute(stmt)).scalar_one_or_none()
    except SQLAlchemyError as exc:
        logger.warning(
            "Could not read desired live state for %s/%s: %s",
            tenant_id,
            greenhouse_id,
            exc,
        )
        return None
    return DesiredScopeState(stored) if stored is not None else None

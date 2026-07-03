"""Dialect-aware bulk upsert helpers for reading tables.

Both Postgres (Supabase) and SQLite (>=3.24, used by the test harness via
aiosqlite) support ``INSERT ... ON CONFLICT DO UPDATE``; the correct insert
construct is picked from the session's dialect at call time.
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import func
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

_DIALECT_INSERTS = {
    "postgresql": postgresql.insert,
    "sqlite": sqlite.insert,
}

READING_CONFLICT_COLS: tuple[str, ...] = ("tenant_id", "greenhouse_id", "timestamp")


def dedupe_rows_by_key(
    rows: list[dict[str, Any]],
    key_cols: Sequence[str] = READING_CONFLICT_COLS,
) -> list[dict[str, Any]]:
    """Keep the last occurrence per conflict key.

    ``ON CONFLICT DO UPDATE`` rejects a statement that touches the same row
    twice, so duplicate keys inside one chunk must collapse before insert.
    """
    by_key: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        by_key[tuple(row[col] for col in key_cols)] = row
    return list(by_key.values())


async def bulk_upsert_rows(
    db: AsyncSession,
    model: type,
    rows: list[dict[str, Any]],
    *,
    update_cols: Sequence[str],
    conflict_cols: Sequence[str] = READING_CONFLICT_COLS,
    preserve_when_null: Sequence[str] = (),
) -> int:
    """Insert rows, updating ``update_cols`` on conflict. Returns rows written.

    Columns in ``preserve_when_null`` keep the existing DB value when the
    incoming value is NULL (used for anomaly_score so re-uploads without
    scores don't wipe previously persisted ones).
    """
    if not rows:
        return 0

    dialect_name = db.bind.dialect.name
    insert_fn = _DIALECT_INSERTS.get(dialect_name)
    if insert_fn is None:
        raise NotImplementedError(f"Upsert not supported for dialect {dialect_name!r}")

    rows = dedupe_rows_by_key(rows, conflict_cols)
    stmt = insert_fn(model).values(rows)
    set_map: dict[str, Any] = {}
    for col in update_cols:
        excluded_col = getattr(stmt.excluded, col)
        if col in preserve_when_null:
            set_map[col] = func.coalesce(excluded_col, getattr(model, col))
        else:
            set_map[col] = excluded_col
    stmt = stmt.on_conflict_do_update(index_elements=list(conflict_cols), set_=set_map)
    await db.execute(stmt)
    return len(rows)

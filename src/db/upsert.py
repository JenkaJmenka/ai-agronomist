"""Dialect-aware bulk upsert helpers for reading tables.

Both Postgres (Supabase) and SQLite (>=3.24, used by the test harness via
aiosqlite) support ``INSERT ... ON CONFLICT DO UPDATE``; the correct insert
construct is picked from the session's dialect at call time.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import func
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from src.db import provenance

_DIALECT_INSERTS = {
    "postgresql": postgresql.insert,
    "sqlite": sqlite.insert,
}

READING_CONFLICT_COLS: tuple[str, ...] = ("tenant_id", "greenhouse_id", "timestamp")

# asyncpg rejects statements with more than 32767 bind parameters; stay well
# below so a multi-VALUES upsert never exceeds the wire-protocol limit.
MAX_BIND_PARAMS = 30000

# Builds the ``SET <col> = <expr>`` expression for one column of an
# ``ON CONFLICT DO UPDATE``, given (incoming value, current DB value). This
# is the generic, reusable mechanism -- it carries no domain knowledge of its
# own; see ``src/db/provenance.py`` for the one domain-specific consumer.
ColumnUpdateExprBuilder = Callable[[ColumnElement, ColumnElement], ColumnElement]


def dialect_insert(db: AsyncSession) -> Callable[..., Any]:
    """Return the dialect-specific SQLAlchemy ``insert()`` constructor for *db*.

    The one place other modules reach for the postgres/sqlite ``insert()``
    split instead of re-deriving ``db.bind.dialect.name`` lookups of their
    own -- e.g. ``src/api/services/synthetic_anomalies.py`` needs plain
    ``ON CONFLICT DO NOTHING`` (not the ``DO UPDATE`` this module's
    ``bulk_upsert_rows`` performs) and shares this lookup rather than
    inventing a second one. Deliberately NOT shared with
    ``src/live/persistence.py``, which keeps its own copy of the same
    mapping -- that module is live-ingestion-specific and reaching into it
    would drag live behaviour into unrelated callers.
    """
    dialect_name = db.bind.dialect.name
    insert_fn = _DIALECT_INSERTS.get(dialect_name)
    if insert_fn is None:
        raise NotImplementedError(f"Unsupported dialect {dialect_name!r}")
    return insert_fn


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
    case_update_cols: Mapping[str, ColumnUpdateExprBuilder] = {},
) -> int:
    """Insert rows, updating ``update_cols`` on conflict. Returns rows written.

    Columns in ``preserve_when_null`` keep the existing DB value when the
    incoming value is NULL (used for anomaly_score so re-uploads without
    scores don't wipe previously persisted ones).

    Columns in ``case_update_cols`` take priority over ``preserve_when_null``:
    each maps to a ``ColumnUpdateExprBuilder`` called with
    ``(incoming_column, existing_column)`` that returns the full ``SET``
    expression for that column -- typically a SQLAlchemy ``case()`` keyed off
    some other existing column's value. A column named here is added to the
    ``SET`` clause even if it is absent from ``update_cols``.

    Provenance declaration: when ``model.PROVENANCE_TRACKED`` is ``True``
    (today only ``SensorReading`` -- see ``src/db/models.py``), every upsert
    through this helper automatically layers in the ADR-010 "last real writer
    owns provenance" rule (``src/db/provenance.py``) on top of whatever
    ``update_cols``/``case_update_cols`` the caller passed. This is
    deliberately NOT opt-in at the call site -- the rule must hold for every
    tracked-model upsert, including ones a future caller adds without knowing
    the rule exists. Pass your own ``case_update_cols`` entry for a
    provenance-covered column to override its automatic expression. A
    provenance-covered column the caller names in ``update_cols`` without an
    explicit ``case_update_cols`` entry is excluded from the automatic rule
    for that call and takes the incoming value unconditionally -- see
    ``provenance.case_cols_for`` for why an explicit update must win over the
    reset.
    """
    if not rows:
        return 0

    insert_fn = dialect_insert(db)

    auto_case_cols: dict[str, ColumnUpdateExprBuilder] = (
        provenance.case_cols_for(model, preserve_when_null, update_cols)
        if getattr(model, "PROVENANCE_TRACKED", False)
        else {}
    )
    merged_case_cols: dict[str, ColumnUpdateExprBuilder] = {
        **auto_case_cols,
        **case_update_cols,
    }
    # dict.fromkeys preserves first-seen order while deduping; a column named
    # in merged_case_cols reaches the SET clause even when the caller never
    # listed it in update_cols.
    covered_cols = list(dict.fromkeys([*update_cols, *merged_case_cols]))

    rows = dedupe_rows_by_key(rows, conflict_cols)
    params_per_row = max(len(row) for row in rows)
    rows_per_stmt = max(1, MAX_BIND_PARAMS // params_per_row)

    for start in range(0, len(rows), rows_per_stmt):
        batch = rows[start : start + rows_per_stmt]
        stmt = insert_fn(model).values(batch)
        set_map: dict[str, Any] = {}
        for col in covered_cols:
            excluded_col = getattr(stmt.excluded, col)
            existing_col = getattr(model, col)
            if col in merged_case_cols:
                set_map[col] = merged_case_cols[col](excluded_col, existing_col)
            elif col in preserve_when_null:
                set_map[col] = func.coalesce(excluded_col, existing_col)
            else:
                set_map[col] = excluded_col
        stmt = stmt.on_conflict_do_update(
            index_elements=list(conflict_cols), set_=set_map
        )
        await db.execute(stmt)
    return len(rows)

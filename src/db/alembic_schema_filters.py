"""Alembic autogenerate filters that exclude schemas owned by other services.

The Auth.js login/session store (``web/lib/auth/schema.sql`` — ``users``,
``accounts``, ``sessions``, ``verification_token``, ``user_tenants``) lives in
a dedicated Postgres schema, fixed at the name below, on the same database
``AUTH_DATABASE_URL`` points at — the same physical database alembic manages
via ``DATABASE_URL``. alembic's own metadata (``src.db.models.Base.metadata``)
knows nothing about those tables.

Without these filters, ``alembic revision --autogenerate`` (run with
``include_schemas=True`` so it can see non-default schemas at all) would
reflect that schema, find its tables unknown to alembic's metadata, and
propose ``DROP TABLE`` for every one of them — silently destroying the
user/session store the next time someone runs autogenerate against the real
database.

Extracted into their own module (rather than inlined in ``alembic/env.py``)
so this logic is directly unit-testable: ``alembic/env.py`` runs
side-effecting top-level code on import (it can trigger a real migration run)
and must never be imported from a test.

The schema name is fixed at ``"auth"`` on both sides of this contract — do
not rename it here without updating ``web/lib/auth/schema.sql`` to match.
"""

from __future__ import annotations

from typing import Any, FrozenSet

# Schemas that belong to another service and must never be touched by this
# app's alembic autogenerate comparison, in either direction (add or drop).
EXCLUDED_SCHEMAS: FrozenSet[str] = frozenset({"auth"})


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """Alembic ``include_name`` hook.

    Called for ``type_ == "schema"`` before alembic reflects anything inside
    it (only relevant when ``include_schemas=True``) — returning False here
    means the schema's tables are never even considered for comparison, so
    they can't be proposed for removal.
    """
    if type_ == "schema":
        return name not in EXCLUDED_SCHEMAS
    return True


def include_object(
    object_: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Alembic ``include_object`` hook — second, independent guard.

    Excludes anything still tagged with an excluded schema that reaches this
    far (e.g. if a future alembic/SQLAlchemy version reorders or changes how
    the two hooks interact). Confined to schema-scoped object types; never
    excludes anything in the app's own (default-schema) tables.
    """
    if type_ == "table":
        table_schema = object_.schema
    elif type_ in ("column", "index", "unique_constraint", "foreign_key_constraint"):
        table_schema = getattr(getattr(object_, "table", None), "schema", None)
    else:
        table_schema = None
    return table_schema not in EXCLUDED_SCHEMAS

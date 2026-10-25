"""Tests for ``src.db.upsert.dialect_insert``.

The postgres/sqlite ``insert()`` split became a public helper when
``synthetic_anomaly_persistence.py`` needed plain ``ON CONFLICT DO NOTHING``
instead of the ``DO UPDATE`` ``bulk_upsert_rows`` performs.  Its two callers
both only ever run against sqlite in tests, so without this module the
postgres arm and the unsupported-dialect guard have no coverage at all —
a sqlite-only suite would stay green with the postgres mapping deleted.

``bulk_upsert_rows`` itself is covered by ``test_bulk_upsert.py`` and
``test_bulk_upsert_provenance.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import create_async_engine

from src.db.upsert import dialect_insert


def _session_bound_to(dialect_name: str) -> MagicMock:
    """A stand-in session whose only relevant attribute is its dialect name.

    ``dialect_insert`` reads nothing but ``db.bind.dialect.name``, and the
    unsupported-dialect case cannot be reached with a real engine the project
    has a driver for.
    """
    session = MagicMock()
    session.bind.dialect.name = dialect_name
    return session


def test_should_return_the_sqlite_insert_for_a_sqlite_session() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session = _session_bound_to(engine.dialect.name)

    assert dialect_insert(session) is sqlite.insert


def test_should_return_the_postgresql_insert_for_a_postgres_session() -> None:
    """Pinned against a real engine's reported dialect name rather than the
    literal "postgresql", so a SQLAlchemy rename cannot pass silently."""
    engine = create_async_engine("postgresql+asyncpg://user:pw@localhost/db")
    session = _session_bound_to(engine.dialect.name)

    assert dialect_insert(session) is postgresql.insert


def test_should_raise_not_implemented_for_an_unsupported_dialect() -> None:
    with pytest.raises(NotImplementedError) as exc_info:
        dialect_insert(_session_bound_to("oracle"))

    assert "oracle" in str(exc_info.value)

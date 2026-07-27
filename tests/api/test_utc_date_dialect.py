"""Regression tests: utc_date SQL compilation differs correctly by dialect.

WHY this test exists
--------------------
SensorReading.timestamp is DateTime(timezone=True) → 'timestamptz' in
PostgreSQL.  func.date(timestamptz) compiles to the SAME SQL text on both
SQLite and Postgres dialects:

    date(sensor_readings.timestamp)

but the *semantics* differ:

  SQLite  : date() operates on the ISO string → always UTC (safe)
  Postgres: date(timestamptz) casts via the session TimeZone GUC, so if
            that GUC is not UTC, day boundaries shift and rows land in the
            wrong date group.

Because the SQL text is identical, no SQLite test ever catches this — it is a
silent production defect.  The fix introduces utc_date(), a dialect-aware
compiled expression that emits:

  Postgres  : date((col) AT TIME ZONE 'UTC')   ← explicit, GUC-independent
  all others: date(col)                         ← SQLite / test path unchanged

This file contains COMPILE-LEVEL tests only (no live database required).
They assert on the generated SQL text because that is the only layer at which
this class of dialect mismatch can be observed without a live Postgres instance.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects import sqlite as sa_sqlite

from src.api.services.anomaly_group_service import utc_date
from src.db.models import SensorReading


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compile(stmt: object, dialect: object) -> str:
    """Compile a SQLAlchemy statement to a SQL string for the given dialect."""
    return str(stmt.compile(dialect=dialect))  # type: ignore[union-attr]


_PG_DIALECT = postgresql.dialect()
_SQLITE_DIALECT = sa_sqlite.dialect()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUtcDatePostgresDialect:
    """PostgreSQL compilation must include an explicit AT TIME ZONE 'UTC'."""

    def test_select_contains_at_time_zone_utc(self) -> None:
        """SELECT utc_date(timestamp) on Postgres → date(... AT TIME ZONE 'UTC').

        This is the critical assertion: the session TimeZone GUC must NOT
        influence date extraction.  Without 'AT TIME ZONE', date(timestamptz)
        on Postgres shifts according to the session timezone, silently putting
        rows in the wrong date group.
        """
        stmt = select(utc_date(SensorReading.timestamp))
        sql = _compile(stmt, _PG_DIALECT)
        assert "AT TIME ZONE 'UTC'" in sql, (
            f"Postgres dialect must emit AT TIME ZONE 'UTC' to pin day boundaries "
            f"to UTC regardless of session TimeZone GUC.\nGot: {sql}"
        )

    def test_select_contains_date_function(self) -> None:
        """The outer function must still be date()."""
        stmt = select(utc_date(SensorReading.timestamp))
        sql = _compile(stmt, _PG_DIALECT)
        assert sql.lower().startswith("select date("), (
            f"Expected SELECT date(...) on Postgres, got: {sql}"
        )

    def test_timestamp_column_is_inside_date_call(self) -> None:
        """The timestamp column reference must appear inside the date() call."""
        stmt = select(utc_date(SensorReading.timestamp))
        sql = _compile(stmt, _PG_DIALECT)
        # The column is rendered as 'sensor_readings.timestamp' in a bare select.
        assert "sensor_readings.timestamp" in sql, (
            f"Expected sensor_readings.timestamp in Postgres SQL: {sql}"
        )

    def test_group_by_contains_at_time_zone_utc(self) -> None:
        """GROUP BY utc_date(...) must also carry the UTC conversion.

        If GROUP BY and SELECT use the same expression object the SQL planner
        may deduplicate them, but the GROUP BY clause must still be explicit so
        that the Postgres planner knows which timezone to use.
        """
        stmt = (
            select(utc_date(SensorReading.timestamp).label("day"))
            .group_by(utc_date(SensorReading.timestamp))
        )
        sql = _compile(stmt, _PG_DIALECT)
        # Both SELECT and GROUP BY should contain the UTC anchor.
        # Count occurrences: at least 2 (one in SELECT, one in GROUP BY).
        occurrences = sql.count("AT TIME ZONE 'UTC'")
        assert occurrences >= 2, (
            f"Expected AT TIME ZONE 'UTC' in both SELECT and GROUP BY for Postgres "
            f"(got {occurrences} occurrence(s)).\nSQL: {sql}"
        )


class TestUtcDateSqliteDialect:
    """SQLite compilation must NOT add AT TIME ZONE (no such function in SQLite)."""

    def test_select_does_not_contain_at_time_zone(self) -> None:
        """SQLite stores ISO strings in UTC — plain date() is correct and safe.

        SQLite has no timezone() function; AT TIME ZONE is a Postgres-ism.
        Emitting it for SQLite would cause a runtime 'no such function' error
        in all unit tests (which use sqlite+aiosqlite).
        """
        stmt = select(utc_date(SensorReading.timestamp))
        sql = _compile(stmt, _SQLITE_DIALECT)
        assert "AT TIME ZONE" not in sql, (
            f"SQLite dialect must NOT emit AT TIME ZONE — SQLite has no such "
            f"function and would raise at runtime.\nGot: {sql}"
        )

    def test_select_is_plain_date_call(self) -> None:
        """SQLite output is unchanged from the original func.date() expression."""
        stmt = select(utc_date(SensorReading.timestamp))
        sql = _compile(stmt, _SQLITE_DIALECT)
        assert sql.lower().startswith("select date("), (
            f"Expected SELECT date(...) unchanged on SQLite, got: {sql}"
        )

    def test_timestamp_column_is_inside_date_call(self) -> None:
        """Sanity: the timestamp column must still be the argument."""
        stmt = select(utc_date(SensorReading.timestamp))
        sql = _compile(stmt, _SQLITE_DIALECT)
        assert "sensor_readings.timestamp" in sql, (
            f"Expected sensor_readings.timestamp in SQLite SQL: {sql}"
        )


class TestUtcDateDialectSymmetry:
    """Cross-dialect invariants: same column, different output, same structure."""

    def test_pg_and_sqlite_both_reference_timestamp_column(self) -> None:
        """Both dialects must reference sensor_readings.timestamp."""
        stmt = select(utc_date(SensorReading.timestamp))
        pg_sql = _compile(stmt, _PG_DIALECT)
        sqlite_sql = _compile(stmt, _SQLITE_DIALECT)
        assert "sensor_readings.timestamp" in pg_sql
        assert "sensor_readings.timestamp" in sqlite_sql

    def test_pg_output_differs_from_sqlite_output(self) -> None:
        """The two dialects must produce different SQL — if they are identical
        the construct is broken and offers no protection against the Postgres GUC bug."""
        stmt = select(utc_date(SensorReading.timestamp))
        pg_sql = _compile(stmt, _PG_DIALECT)
        sqlite_sql = _compile(stmt, _SQLITE_DIALECT)
        assert pg_sql != sqlite_sql, (
            "Postgres and SQLite SQL must differ — identical output means the "
            "timezone fix is not being applied on Postgres.\n"
            f"Both produced: {pg_sql}"
        )

    def test_inherit_cache_is_set(self) -> None:
        """inherit_cache=True is required by SQLAlchemy 2.x to enable the
        compiled-query cache.  Without it every query bypasses the cache and
        SQLAlchemy emits a warning; in high-traffic paths this causes a
        significant performance regression."""
        assert utc_date.inherit_cache is True, (
            "utc_date.inherit_cache must be True for SQLAlchemy 2.x query caching"
        )

"""Tests for migration 023 — canonical anomaly score.

Tests the upgrade/downgrade logic directly using SQLite in-memory databases.
These tests do NOT use the alembic CLI (which is broken in this environment)
and therefore cannot test the alembic version table or env.py.  They exercise
the Python functions that implement the backfill and verify the correctness
guarantees from ADR-005.

SQLite-only limitation (documented per task instructions)
---------------------------------------------------------
These tests run against SQLite because alembic's env.py is not available in
this environment.  The non-finite (NaN / ±Inf) guard in step 4 of the backfill
is exercised here but CANNOT prove Postgres behaviour: Postgres float8 accepts
NaN/Infinity as stored values, and only fails when those values are serialised
to JSON (e.g. in a JSON column or response body).  SQLite's type affinity
accepts NaN just as well and will not reproduce the JSON 500 failure mode.
A Postgres-specific integration test against a real Postgres instance is
required to verify the NaN guard end-to-end.
"""

from __future__ import annotations

import math
from typing import Optional

import pytest
import sqlalchemy as sa
from sqlalchemy import text


# ---------------------------------------------------------------------------
# Inline copies of the migration helpers (from alembic/versions/023_*.py).
# We test the functions directly rather than running alembic commands.
# ---------------------------------------------------------------------------

def _canonical(raw: float, p: float, hi: Optional[float]) -> Optional[float]:
    if not math.isfinite(raw) or not math.isfinite(p) or p <= 0.0:
        return None
    if hi is not None and not math.isfinite(hi):
        return None
    if raw < 0.0:
        return 0.0
    if raw <= p:
        return 0.5 * raw / p
    if hi is not None:
        return 0.5 + 0.5 * (raw - p) / (hi - p)
    return 0.5 + 0.5 * (raw - p) / raw


def _legacy_map(raw: float) -> Optional[float]:
    if not math.isfinite(raw) or raw < 0.0:
        return None
    return 0.5 + 0.5 * raw / (1.0 + raw)


# ---------------------------------------------------------------------------
# Fixture: SQLite in-memory DB with a minimal sensor_readings table
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn():
    """Provide a synchronous SQLite in-memory connection with the schema."""
    engine = sa.create_engine("sqlite:///:memory:", echo=False)
    with engine.connect() as connection:
        connection.execute(text("""
            CREATE TABLE sensor_readings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id       TEXT NOT NULL,
                greenhouse_id   TEXT NOT NULL DEFAULT 'default',
                timestamp       TEXT NOT NULL,
                temperature     REAL NOT NULL DEFAULT 0.0,
                humidity        REAL NOT NULL DEFAULT 0.0,
                co2             REAL NOT NULL DEFAULT 0.0,
                light           REAL NOT NULL DEFAULT 0.0,
                anomaly_score   REAL,
                anomaly_type    TEXT,
                source          TEXT
            )
        """))
        connection.commit()
        yield connection


def _add_columns(conn):
    """Simulate the ADD COLUMN DDL from migration 023 upgrade."""
    conn.execute(text("ALTER TABLE sensor_readings ADD COLUMN anomaly_score_raw REAL"))
    conn.execute(text("ALTER TABLE sensor_readings ADD COLUMN score_scale TEXT"))
    conn.commit()


def _insert(
    conn,
    *,
    id: int,
    anomaly_score: Optional[float],
    source: Optional[str] = None,
    anomaly_type: Optional[str] = None,
    ts: str = "2026-01-01T00:00:00Z",
):
    conn.execute(
        text(
            "INSERT INTO sensor_readings "
            "(id, tenant_id, greenhouse_id, timestamp, temperature, humidity, co2, light, "
            " anomaly_score, source, anomaly_type) "
            "VALUES (:id, 't1', 'g1', :ts, 20.0, 60.0, 400.0, 100.0, "
            "        :score, :source, :atype)"
        ),
        {"id": id, "ts": ts, "score": anomaly_score, "source": source, "atype": anomaly_type},
    )


def _fetch(conn, row_id: int) -> dict:
    row = conn.execute(
        text("SELECT anomaly_score, anomaly_score_raw, score_scale FROM sensor_readings WHERE id = :id"),
        {"id": row_id},
    ).fetchone()
    return {
        "anomaly_score": row[0],
        "anomaly_score_raw": row[1],
        "score_scale": row[2],
    }


# ---------------------------------------------------------------------------
# Inline backfill runners (mirror migration logic exactly)
# ---------------------------------------------------------------------------

_BATCH = 500
_LIVE_P = 0.05
_SYNTHETIC_FILLER = 0.10
_SYNTHETIC_INJECTED = 0.85
_SYNTHETIC_CONTEXT_TYPE = "synthetic_context"


def _run_backfill_live(conn):
    while True:
        rows = conn.execute(
            text(
                "SELECT id, anomaly_score FROM sensor_readings "
                "WHERE source = 'live' AND score_scale IS NULL "
                f"AND anomaly_score IS NOT NULL LIMIT {_BATCH}"
            )
        ).fetchall()
        if not rows:
            break
        params = []
        for row_id, raw in rows:
            raw_f = float(raw) if raw is not None else float("nan")
            if not math.isfinite(raw_f):
                params.append({"id": row_id, "canonical": None, "raw": None, "scale": "unknown"})
            else:
                can = _canonical(raw_f, _LIVE_P, None)
                params.append({
                    "id": row_id,
                    "canonical": can,
                    "raw": raw_f,
                    "scale": "live" if can is not None else "unknown",
                })
        conn.execute(
            text(
                "UPDATE sensor_readings SET anomaly_score = :canonical, "
                "anomaly_score_raw = :raw, score_scale = :scale WHERE id = :id"
            ),
            params,
        )
        conn.commit()


def _run_backfill_synthetic(conn):
    while True:
        rows = conn.execute(
            text(
                "SELECT id, anomaly_type FROM sensor_readings "
                f"WHERE anomaly_type IS NOT NULL AND score_scale IS NULL LIMIT {_BATCH}"
            )
        ).fetchall()
        if not rows:
            break
        params = []
        for row_id, atype in rows:
            score = _SYNTHETIC_FILLER if atype == _SYNTHETIC_CONTEXT_TYPE else _SYNTHETIC_INJECTED
            params.append({"id": row_id, "canonical": score, "raw": None, "scale": "synthetic"})
        conn.execute(
            text(
                "UPDATE sensor_readings SET anomaly_score = :canonical, "
                "anomaly_score_raw = :raw, score_scale = :scale WHERE id = :id"
            ),
            params,
        )
        conn.commit()


def _run_backfill_legacy(conn):
    while True:
        rows = conn.execute(
            text(
                "SELECT id, anomaly_score FROM sensor_readings "
                f"WHERE score_scale IS NULL AND anomaly_score IS NOT NULL LIMIT {_BATCH}"
            )
        ).fetchall()
        if not rows:
            break
        params = []
        for row_id, raw in rows:
            raw_f = float(raw) if raw is not None else float("nan")
            if not math.isfinite(raw_f):
                params.append({"id": row_id, "canonical": None, "raw": None, "scale": "unknown"})
            else:
                can = _legacy_map(raw_f)
                params.append({
                    "id": row_id,
                    "canonical": can,
                    "raw": raw_f,
                    "scale": "legacy" if can is not None else "unknown",
                })
        conn.execute(
            text(
                "UPDATE sensor_readings SET anomaly_score = :canonical, "
                "anomaly_score_raw = :raw, score_scale = :scale WHERE id = :id"
            ),
            params,
        )
        conn.commit()


def _run_full_upgrade_backfill(conn):
    _run_backfill_live(conn)
    _run_backfill_synthetic(conn)
    _run_backfill_legacy(conn)


def _run_downgrade(conn):
    while True:
        rows = conn.execute(
            text(
                f"SELECT id, anomaly_score_raw FROM sensor_readings "
                f"WHERE anomaly_score_raw IS NOT NULL LIMIT {_BATCH}"
            )
        ).fetchall()
        if not rows:
            break
        params = [{"id": r[0], "raw": r[1]} for r in rows]
        conn.execute(
            text("UPDATE sensor_readings SET anomaly_score = :raw WHERE id = :id"),
            params,
        )
        conn.commit()
    conn.execute(text("ALTER TABLE sensor_readings DROP COLUMN score_scale"))
    conn.execute(text("ALTER TABLE sensor_readings DROP COLUMN anomaly_score_raw"))
    conn.commit()


# ---------------------------------------------------------------------------
# Cohort tests
# ---------------------------------------------------------------------------

class TestLiveCohortBackfill:

    def test_noise_floor_transforms_correctly(self, conn):
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=0.017, source="live")
        conn.commit()

        _run_backfill_live(conn)
        row = _fetch(conn, 1)

        assert row["score_scale"] == "live"
        assert abs(row["anomaly_score_raw"] - 0.017) < 1e-9
        # canonical(0.017, 0.05, None) = 0.5 * 0.017 / 0.05 = 0.17
        assert abs(row["anomaly_score"] - 0.17) < 1e-9

    def test_at_threshold_gives_half(self, conn):
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=0.05, source="live")
        conn.commit()
        _run_backfill_live(conn)
        row = _fetch(conn, 1)
        assert abs(row["anomaly_score"] - 0.5) < 1e-9
        assert row["score_scale"] == "live"

    def test_null_score_left_alone(self, conn):
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=None, source="live")
        conn.commit()
        _run_backfill_live(conn)
        row = _fetch(conn, 1)
        assert row["anomaly_score"] is None
        assert row["score_scale"] is None  # intentionally unscored


class TestSyntheticCohortBackfill:

    def test_filler_maps_to_010(self, conn):
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=0.15, anomaly_type="synthetic_context")
        conn.commit()
        _run_backfill_synthetic(conn)
        row = _fetch(conn, 1)
        assert abs(row["anomaly_score"] - 0.10) < 1e-9
        assert row["score_scale"] == "synthetic"
        assert row["anomaly_score_raw"] is None

    def test_injected_maps_to_085(self, conn):
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=0.85, anomaly_type="temperature_spike")
        conn.commit()
        _run_backfill_synthetic(conn)
        row = _fetch(conn, 1)
        assert abs(row["anomaly_score"] - 0.85) < 1e-9
        assert row["score_scale"] == "synthetic"


class TestLegacyCohortBackfill:

    def test_legacy_map_is_monotone_and_above_half(self, conn):
        """Every legacy row with a non-zero score must land in (0.5, 1.0)."""
        _add_columns(conn)
        for i, score in enumerate([0.1, 0.5, 0.8, 0.99], start=1):
            _insert(conn, id=i, anomaly_score=score, source=None, anomaly_type=None)
        conn.commit()
        _run_backfill_legacy(conn)

        prev = 0.5
        for i, _ in enumerate([0.1, 0.5, 0.8, 0.99], start=1):
            row = _fetch(conn, i)
            assert row["score_scale"] == "legacy"
            can = row["anomaly_score"]
            assert can is not None
            assert can > 0.5, f"Legacy row must be > 0.5, got {can}"
            assert can < 1.0, f"Legacy row must be < 1.0, got {can}"
            assert can >= prev, f"Monotonicity violated: {can} < {prev}"
            prev = can


# ---------------------------------------------------------------------------
# Idempotency — running the backfill twice must not double-transform
# ---------------------------------------------------------------------------

class TestIdempotency:

    def test_running_backfill_twice_is_idempotent(self, conn):
        """score_scale IS NULL is the idempotency key — proved here."""
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=0.017, source="live")
        conn.commit()

        _run_backfill_live(conn)
        after_first = _fetch(conn, 1)

        # Second run — score_scale is 'live', so this row is skipped.
        _run_backfill_live(conn)
        after_second = _fetch(conn, 1)

        assert after_first["anomaly_score"] == after_second["anomaly_score"], (
            "Running backfill twice should not change anomaly_score"
        )
        assert after_first["score_scale"] == after_second["score_scale"]

    def test_full_backfill_idempotent_mixed(self, conn):
        """Mixed cohorts — all three backfill passes combined, run twice."""
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=0.05, source="live")
        _insert(conn, id=2, anomaly_score=0.80, anomaly_type="temperature_spike")
        _insert(conn, id=3, anomaly_score=0.60, source=None, anomaly_type=None)
        conn.commit()

        _run_full_upgrade_backfill(conn)
        snapshot = {i: _fetch(conn, i) for i in [1, 2, 3]}

        _run_full_upgrade_backfill(conn)
        after_second = {i: _fetch(conn, i) for i in [1, 2, 3]}

        for row_id in [1, 2, 3]:
            assert snapshot[row_id]["anomaly_score"] == after_second[row_id]["anomaly_score"], (
                f"Row {row_id}: anomaly_score changed on second run"
            )


# ---------------------------------------------------------------------------
# Upgrade / downgrade round-trip
# ---------------------------------------------------------------------------

class TestUpgradeDowngradeRoundTrip:

    def test_live_round_trip_restores_original_score(self, conn):
        """Downgrade restores anomaly_score from anomaly_score_raw."""
        original_score = 0.017
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=original_score, source="live")
        conn.commit()

        # Upgrade
        _run_full_upgrade_backfill(conn)
        upgraded = _fetch(conn, 1)
        assert upgraded["score_scale"] == "live"
        assert upgraded["anomaly_score"] != original_score  # transformed

        # Downgrade
        _run_downgrade(conn)
        restored = conn.execute(
            text("SELECT anomaly_score FROM sensor_readings WHERE id = 1")
        ).fetchone()
        assert abs(restored[0] - original_score) < 1e-9, (
            f"Downgrade should restore {original_score}, got {restored[0]}"
        )

    def test_downgrade_drops_new_columns(self, conn):
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=0.5, source="live")
        conn.commit()
        _run_full_upgrade_backfill(conn)
        _run_downgrade(conn)

        # The new columns must no longer exist.
        cols = [
            row[1]
            for row in conn.execute(text("PRAGMA table_info(sensor_readings)")).fetchall()
        ]
        assert "anomaly_score_raw" not in cols
        assert "score_scale" not in cols

    def test_null_score_rows_survive_round_trip(self, conn):
        """Rows that were never scored (anomaly_score = NULL) survive intact."""
        _add_columns(conn)
        _insert(conn, id=1, anomaly_score=None, source="live")
        conn.commit()
        _run_full_upgrade_backfill(conn)
        _run_downgrade(conn)
        result = conn.execute(
            text("SELECT anomaly_score FROM sensor_readings WHERE id = 1")
        ).fetchone()
        assert result[0] is None  # still NULL after round trip


# ---------------------------------------------------------------------------
# Non-finite guard (§2.4 / step 4)
#
# SQLite-only caveat: SQLite's JSON handling differs from Postgres.  A NaN
# stored in a SQLite float column is readable, but Postgres float8 would also
# accept it.  The critical failure mode (JSON 500) only occurs when a NaN value
# reaches a JSON serialiser or a jsonb column in Postgres, which cannot be
# reproduced with SQLite.  These tests verify the Python-level guard only.
# ---------------------------------------------------------------------------

class TestNonFiniteGuard:

    def test_nan_in_live_row_becomes_null_with_unknown_scale(self, conn):
        """NaN anomaly_score in a live row is neutralised by the backfill."""
        _add_columns(conn)
        # SQLite will store NaN as a float; Python represents it as float('nan').
        # We insert it directly via the parameter to avoid SQLite's NaN handling.
        conn.execute(
            text(
                "INSERT INTO sensor_readings "
                "(id, tenant_id, greenhouse_id, timestamp, temperature, humidity, co2, light, "
                " anomaly_score, source) "
                "VALUES (1, 't1', 'g1', '2026-01-01', 20, 60, 400, 100, :score, 'live')"
            ),
            {"score": float("nan")},
        )
        conn.commit()

        _run_backfill_live(conn)
        row = _fetch(conn, 1)

        # The non-finite guard should produce NULL score and 'unknown' scale.
        assert row["score_scale"] == "unknown", f"Expected 'unknown', got {row['score_scale']}"
        assert row["anomaly_score"] is None, f"Expected NULL, got {row['anomaly_score']}"
        # SQLite-only note: in Postgres, this guards against the JSON 500 failure;
        # the SQLite test proves the Python logic is correct but cannot simulate
        # Postgres's JSON serialisation failure.

    def test_canonical_function_returns_none_for_nan(self):
        """Unit test the _canonical helper directly for non-finite inputs."""
        assert _canonical(float("nan"), 0.05, None) is None
        assert _canonical(float("inf"), 0.05, None) is None
        assert _canonical(float("-inf"), 0.05, None) is None
        assert _canonical(0.05, float("nan"), None) is None
        assert _canonical(0.05, 0.0, None) is None
        assert _canonical(0.05, -0.01, None) is None


# ---------------------------------------------------------------------------
# Legacy map properties
# ---------------------------------------------------------------------------

class TestLegacyMap:

    def test_zero_maps_to_half(self):
        assert abs(_legacy_map(0.0) - 0.5) < 1e-9

    def test_above_zero_maps_above_half(self):
        for r in [0.01, 0.1, 0.5, 0.8, 0.99, 5.0]:
            result = _legacy_map(r)
            assert result is not None
            assert result > 0.5, f"_legacy_map({r}) = {result} ≤ 0.5"
            assert result < 1.0, f"_legacy_map({r}) = {result} ≥ 1.0"

    def test_monotone(self):
        values = [0.01 * i for i in range(0, 101)]
        prev = _legacy_map(values[0])
        for v in values[1:]:
            curr = _legacy_map(v)
            assert curr is not None
            assert prev is not None
            assert curr >= prev, f"_legacy_map not monotone: {prev} > {curr}"
            prev = curr

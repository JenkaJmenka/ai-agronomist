"""Add canonical anomaly score columns and backfill existing rows.

Revision ID: 023
Revises: 022
Create Date: 2026-08-02

Background — ADR-005
--------------------
``SensorReading.anomaly_score`` was written by four producers on three
incompatible numeric scales (live raw recon error ≤ 0.36, batch fused
probability ≤ 1.0, synthetic fabricated 0.02–0.99, upload sparse above P99.9).
A single FE threshold value cannot be correct for all of them.

ADR-005 §2.1 introduces a canonical scale where ``0.5`` is every producer's
own decision point.  The transform is applied at write time so the read path
needs no changes:

    canonical(raw, p, hi):
        raw < 0                   →  0.0
        raw <= p                  →  0.5 * raw / p
        raw >  p, hi is not None  →  0.5 + 0.5 * (raw - p) / (hi - p)
        raw >  p, hi is None      →  0.5 + 0.5 * (raw - p) / raw

New columns
-----------
* ``anomaly_score_raw FLOAT NULL``   — producer's native value verbatim.
* ``score_scale VARCHAR(16) NULL``   — provenance: live | upload | batch |
                                       synthetic | legacy | unknown.

Step 1: add columns (DDL)
--------------------------
Both are nullable with no default → metadata-only DDL on Postgres, no table
rewrite, no long lock.  Each column is added only when absent, so a database
left half-upgraded by the earlier commit-per-batch version of this migration
(columns present, revision unstamped) resumes instead of failing on
DuplicateColumnError.

Step 2–4: batched backfill (idempotent via score_scale IS NULL)
--------------------------------------------------------------
Batching bounds how many rows are held in memory at once; it does NOT commit.
An earlier version called ``connection.commit()`` per batch, which is invalid
inside the transaction alembic opens around a migration — the next statement
died with "Can't operate on closed transaction inside context manager", after
the commit had already made Step 1's DDL permanent.  That is what left
production half-upgraded.  The whole migration is now one transaction: it
either lands completely or rolls back.  The loops still terminate because each
UPDATE sets score_scale, and a transaction sees its own writes.

Cohort rules (§5.2):

live
    source = 'live'.  (p=0.05, hi=None).  Canonical transform applied.

upload / batch (legacy)
    anomaly_score IS NOT NULL, score_scale IS NULL, source != 'live',
    anomaly_type IS NULL.  Per-file ``p`` is unrecoverable, but sparseness
    guarantees every stored score was above its own cutoff → use a
    rank-preserving monotone map into (0.5, 1.0):
        0.5 + 0.5 * raw / (1 + raw)
    score_scale = 'legacy' (not 'upload') so approximated rows are
    distinguishable from correctly-calibrated ones written after this change.

synthetic (escape hatch, default)
    anomaly_type IS NOT NULL.  The ADR prescribes DELETE but notes an escape
    hatch for demo tenants.  This migration implements the ESCAPE HATCH as the
    default to avoid deleting tenant data without an explicit human decision.
    Filler rows (anomaly_type = 'synthetic_context') → anomaly_score = 0.10.
    Injected fault rows                              → anomaly_score = 0.85.
    score_scale = 'synthetic'.
    anomaly_score_raw = NULL (ground-truth rows have no detector raw value).
    See summary note at end of module docstring.

non-finite (step 4)
    anomaly_score IS NaN or ±Inf.  Set anomaly_score = NULL,
    score_scale = 'unknown'.  Postgres float8 accepts NaN/Infinity; feeding
    them through the transform propagates into JSON responses that 500.

Step 5: reindex (MANUAL RUNBOOK — do NOT run inside this migration)
-------------------------------------------------------------------
``ix_sensor_readings_score_desc`` (migration 022) is a partial index
``WHERE anomaly_score IS NOT NULL``.  Rewriting every stored value bloats the
index and leaves the planner with stale statistics.  After backfill completes,
schedule the following as a manual DBA step during a maintenance window:

    -- 1. Build the replacement index without blocking reads/writes.
    CREATE INDEX CONCURRENTLY ix_sensor_readings_score_desc_new
        ON sensor_readings (tenant_id, greenhouse_id, anomaly_score DESC)
        WHERE anomaly_score IS NOT NULL;

    -- 2. Swap atomically.
    BEGIN;
    DROP INDEX ix_sensor_readings_score_desc;
    ALTER INDEX ix_sensor_readings_score_desc_new
        RENAME TO ix_sensor_readings_score_desc;
    COMMIT;

    -- 3. Update planner statistics.
    ANALYZE sensor_readings;

WARNING: CREATE INDEX CONCURRENTLY cannot run inside a transaction block, which
is why it is a manual runbook step and NOT part of this migration.

Step 6: downgrade
-----------------
Restore ``anomaly_score := anomaly_score_raw`` for rows where raw is present,
then drop both new columns.  The raw column is what makes this reversible.

Escape hatch note
-----------------
The ADR §5.2 prescribes DELETE for the synthetic cohort and lists an escape
hatch for demo tenants.  This migration implements the escape hatch as the
DEFAULT because deleting rows in a migration is not reversible by the downgrade
path, and removing tenant data without an explicit human decision is higher risk
than keeping approximate values.  If a future operator decides the synthetic rows
should be deleted, they can do so via the existing DELETE /inject/anomalies
endpoint, which is designed for exactly this and writes a proper audit trail.
"""

from __future__ import annotations

import math
from typing import Optional

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Calibration constants (inline — do NOT import from src.config here to keep
# the migration self-contained and re-runnable without the application context).
# These values MUST match SCORE_SCALE_CALIBRATION in src/config.py.
# ---------------------------------------------------------------------------
_LIVE_P: float = 0.05    # LIVE_ANOMALY_THRESHOLD
_LIVE_HI: Optional[float] = None  # unbounded

# Synthetic escape-hatch canonical values.
_SYNTHETIC_FILLER_SCORE: float = 0.10
_SYNTHETIC_INJECTED_SCORE: float = 0.85
_SYNTHETIC_CONTEXT_TYPE: str = "synthetic_context"

_BATCH_SIZE: int = 1_000


# ---------------------------------------------------------------------------
# Inline canonical transform (mirrors src/api/services/score_transform.py).
# Do not import from there — migrations must be self-contained.
# ---------------------------------------------------------------------------

def _canonical(raw: float, p: float, hi: Optional[float]) -> Optional[float]:
    """Four-branch piecewise canonical transform (ADR-005 §2.1)."""
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
    """Rank-preserving monotone map into (0.5, 1.0) for upload/batch legacy rows.

    Every stored upload/batch score was above its own cutoff (sparseness
    guarantees this).  0.5 + 0.5 * raw / (1 + raw) is:
    - Monotonically increasing in raw
    - Bounded in (0.5, 1.0) for raw > 0
    - Continuous at raw = 0 (maps to 0.5)
    """
    if not math.isfinite(raw) or raw < 0.0:
        return None
    return 0.5 + 0.5 * raw / (1.0 + raw)


def _is_nonfinite(score: float) -> bool:
    """True for NaN, +Inf, -Inf."""
    return not math.isfinite(score)


# ---------------------------------------------------------------------------
# Batched backfill helpers
# ---------------------------------------------------------------------------

def _backfill_live(connection: sa.engine.Connection) -> int:
    """Backfill live cohort (source = 'live').  Returns rows updated."""
    updated = 0
    while True:
        rows = connection.execute(
            text(
                "SELECT id, anomaly_score FROM sensor_readings "
                "WHERE source = 'live' AND score_scale IS NULL "
                "AND anomaly_score IS NOT NULL "
                f"LIMIT {_BATCH_SIZE}"
            )
        ).fetchall()
        if not rows:
            break

        params = []
        for row_id, raw in rows:
            try:
                raw_f = float(raw)
            except (TypeError, ValueError):
                raw_f = float("nan")

            if _is_nonfinite(raw_f):
                params.append({
                    "id": row_id,
                    "canonical": None,
                    "raw": None,
                    "scale": "unknown",
                })
            else:
                can = _canonical(raw_f, _LIVE_P, _LIVE_HI)
                params.append({
                    "id": row_id,
                    "canonical": can,
                    "raw": raw_f,
                    "scale": "live" if can is not None else "unknown",
                })

        connection.execute(
            text(
                "UPDATE sensor_readings "
                "SET anomaly_score = :canonical, "
                "    anomaly_score_raw = :raw, "
                "    score_scale = :scale "
                "WHERE id = :id"
            ),
            params,
        )
        updated += len(rows)
    return updated


def _backfill_synthetic(connection: sa.engine.Connection) -> int:
    """Backfill synthetic cohort (anomaly_type IS NOT NULL).

    Implements the escape hatch (not DELETE) — see module docstring.
    Filler rows → 0.10, injected fault rows → 0.85.

    Safe to overwrite every matching row with a flat score despite keying
    off ``anomaly_type IS NOT NULL`` alone (no source/provenance check):
    this migration only ever runs on a database below revision 023, and
    real anomaly detection cannot have written ``anomaly_type`` yet — that
    lands no earlier than migration 026 (see ADR-010's ordering constraint
    and fault_classification.py). So at the point this runs,
    ``anomaly_type IS NOT NULL`` is still exactly the synthetic-injection
    cohort with no real-detection rows mixed in. Anyone reordering the
    migration chain so a real-detection writer lands before revision 023
    breaks this invariant and must revisit this backfill.
    """
    updated = 0
    while True:
        rows = connection.execute(
            text(
                "SELECT id, anomaly_type FROM sensor_readings "
                "WHERE anomaly_type IS NOT NULL AND score_scale IS NULL "
                f"LIMIT {_BATCH_SIZE}"
            )
        ).fetchall()
        if not rows:
            break

        params = []
        for row_id, atype in rows:
            score = (
                _SYNTHETIC_FILLER_SCORE
                if atype == _SYNTHETIC_CONTEXT_TYPE
                else _SYNTHETIC_INJECTED_SCORE
            )
            params.append({
                "id": row_id,
                "canonical": score,
                "raw": None,  # ground-truth: no detector raw value
                "scale": "synthetic",
            })

        connection.execute(
            text(
                "UPDATE sensor_readings "
                "SET anomaly_score = :canonical, "
                "    anomaly_score_raw = :raw, "
                "    score_scale = :scale "
                "WHERE id = :id"
            ),
            params,
        )
        updated += len(rows)
    return updated


def _backfill_legacy(connection: sa.engine.Connection) -> int:
    """Backfill upload/batch legacy cohort.

    Rows with anomaly_score IS NOT NULL and still no score_scale after live
    and synthetic passes are assumed to be upload or batch origin.  Per-file
    threshold is unrecoverable; apply the rank-preserving legacy map.
    """
    updated = 0
    while True:
        rows = connection.execute(
            text(
                "SELECT id, anomaly_score FROM sensor_readings "
                "WHERE score_scale IS NULL AND anomaly_score IS NOT NULL "
                f"LIMIT {_BATCH_SIZE}"
            )
        ).fetchall()
        if not rows:
            break

        params = []
        for row_id, raw in rows:
            try:
                raw_f = float(raw)
            except (TypeError, ValueError):
                raw_f = float("nan")

            if _is_nonfinite(raw_f):
                params.append({
                    "id": row_id,
                    "canonical": None,
                    "raw": None,
                    "scale": "unknown",
                })
            else:
                can = _legacy_map(raw_f)
                params.append({
                    "id": row_id,
                    "canonical": can,
                    "raw": raw_f,
                    "scale": "legacy" if can is not None else "unknown",
                })

        connection.execute(
            text(
                "UPDATE sensor_readings "
                "SET anomaly_score = :canonical, "
                "    anomaly_score_raw = :raw, "
                "    score_scale = :scale "
                "WHERE id = :id"
            ),
            params,
        )
        updated += len(rows)
    return updated


def _backfill_nonfinite_remaining(connection: sa.engine.Connection) -> int:
    """Catch-all: set score_scale for any remaining NULL-score rows.

    Rows where anomaly_score IS NULL (no score ever assigned) get
    score_scale = NULL left alone — they are intentionally unscored.
    This pass only tags rows where anomaly_score somehow remained NULL
    during the backfill (e.g. due to a NaN that couldn't be detected
    pre-backfill).  score_scale IS NULL is the idempotency key.
    """
    # Nothing to do — rows with anomaly_score = NULL are intentionally left
    # with score_scale = NULL; they were never scored and should not be labelled.
    # The batched passes above already handled NaN / Inf by setting score_scale
    # = 'unknown'.  No further action needed.
    return 0


# ---------------------------------------------------------------------------
# Public upgrade / downgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    # The earlier commit-per-batch version of this migration committed Step 1's
    # DDL before dying on the first statement after that commit, so production
    # was left with the columns present and the revision unstamped.  A plain
    # add_column then raised DuplicateColumnError on every subsequent deploy,
    # which blocked the API — and with it migration 024 — for two days.  Adding
    # only what is missing lets such a database resume; the commits themselves
    # are gone (see the Step 2–4 note in the module docstring).
    # get_bind() is the connection inside the alembic migration context.
    connection = op.get_bind()
    existing = {
        col["name"] for col in sa.inspect(connection).get_columns("sensor_readings")
    }

    # Step 1 — Add columns.
    # Both nullable with no default → metadata-only DDL on Postgres.
    if "anomaly_score_raw" not in existing:
        op.add_column(
            "sensor_readings",
            sa.Column("anomaly_score_raw", sa.Float(), nullable=True),
        )
    if "score_scale" not in existing:
        op.add_column(
            "sensor_readings",
            sa.Column("score_scale", sa.String(16), nullable=True),
        )

    # Live cohort first (most valuable provenance).
    _backfill_live(connection)

    # Synthetic cohort (escape hatch — see module docstring).
    _backfill_synthetic(connection)

    # Legacy upload/batch cohort — everything else with a non-NULL score.
    _backfill_legacy(connection)

    # Remaining null-score rows require no action — see helper docstring.
    _backfill_nonfinite_remaining(connection)

    # Step 5 — REINDEX is a MANUAL RUNBOOK STEP.
    # Do not run CREATE INDEX CONCURRENTLY here — it cannot execute inside a
    # transaction block and would fail on Postgres.  See module docstring for
    # the full 3-step runbook.


def downgrade() -> None:
    # Step 6 — Restore anomaly_score from anomaly_score_raw then drop columns.
    connection = op.get_bind()

    # Restore rows that have a raw value.
    while True:
        rows = connection.execute(
            text(
                "SELECT id, anomaly_score_raw FROM sensor_readings "
                "WHERE anomaly_score_raw IS NOT NULL "
                f"LIMIT {_BATCH_SIZE}"
            )
        ).fetchall()
        if not rows:
            break

        params = [{"id": row_id, "raw": raw} for row_id, raw in rows]
        connection.execute(
            text(
                "UPDATE sensor_readings "
                # Also clear anomaly_score_raw so the next batch's
                # WHERE anomaly_score_raw IS NOT NULL does not re-select
                # the same rows — without this the loop never terminates.
                "SET anomaly_score = :raw, anomaly_score_raw = NULL "
                "WHERE id = :id"
            ),
            params,
        )

    op.drop_column("sensor_readings", "score_scale")
    op.drop_column("sensor_readings", "anomaly_score_raw")

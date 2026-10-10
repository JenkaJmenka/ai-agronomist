"""Backfill the synthetic:<type> source marker onto pre-existing rows.

Revision ID: 025
Revises: 024
Create Date: 2026-10-04

Background
----------
``sensor_readings.anomaly_type`` has exactly one writer of a non-NULL value
today: the inject-test endpoint (``src/api/services/synthetic_anomalies.py``).
That coincidence has let two call sites treat ``anomaly_type IS NOT NULL`` as
a de-facto "this row is synthetic" flag (``delete_synthetic_anomalies`` and
``wipe_live_demo_data``'s fallback branch). Real anomaly detection is about to
start persisting ``anomaly_type`` too, which would make those call sites
destroy or leak genuine detections. See
``docs/architecture/ADR-010-synthetic-provenance-marker.md``.

The fix moves the "is synthetic" signal onto ``SensorReading.source``, set to
``'synthetic:<type>'`` (``'synthetic:context'`` for the background filler
rows), mirroring the ``RawReading`` convention already in place at
``synthetic_anomalies.py``. The ``source`` column itself already exists as of
migration 017 — this migration is backfill-only, no DDL.

(Note: ``src/config.py`` now exposes ``SYNTHETIC_SOURCE_PREFIX`` /
``synthetic_source()`` for application code to build this marker. This
migration intentionally does NOT import them — a migration must stay valid
against the code as it was written; importing an app constant would let a
later rename silently rewrite migration history. The ``'synthetic:'``
literal below is a deliberate, cross-referenced duplicate.)

The predicate: why score_scale='synthetic' alone, or anomaly_type IS NOT
NULL alone, are each insufficient
-------------------------------------------------------------------------
An earlier draft of this migration used
``WHERE source IS NULL AND (anomaly_type IS NOT NULL OR score_scale =
'synthetic')``. That is a DATA-LOSS BUG, not merely imprecise: this
migration ships a tested ``downgrade()``, so re-running it (downgrade then
upgrade) is routine operational practice, not a hypothetical. Once real
anomaly detection starts persisting ``anomaly_type`` (with ``source IS
NULL``, because ``csv_persist.apply_anomaly_scores`` never sets ``source``),
a ``downgrade 024`` followed by ``upgrade head`` would tag every genuine
detection ``synthetic:<LABEL>``, and the next "Remove all synthetic" would
delete them.

``score_scale = 'synthetic'`` alone is ALSO insufficient on its own: a
batch-inferred row that started out as a synthetic injection can lose its
``score_scale='synthetic'`` without losing its synthetic identity.
``src/api/routes/inference.py`` selects ``SensorReading`` rows by
tenant/greenhouse/timestamp with no provenance filter and unconditionally
sets ``score_scale = "batch"`` on a match (see the migration-window guard
added there alongside this one) — so a batch-inferred injected row would
otherwise lose its only marker and become permanently undeletable by
``delete_synthetic_anomalies``.

The fix: corroborate with the sibling RawReading
-------------------------------------------------
    WHERE source IS NULL
      AND (score_scale = 'synthetic' OR EXISTS (
             SELECT 1 FROM raw_readings r
              WHERE r.tenant_id = sensor_readings.tenant_id
                AND r.greenhouse_id = sensor_readings.greenhouse_id
                AND r.timestamp = sensor_readings.timestamp
                AND r.source LIKE 'synthetic:%'
          ))

``inject_synthetic_anomalies`` always writes a ``RawReading`` row alongside
each ``SensorReading`` row it creates (when the tenant has raw-column history
to learn ranges from — see ``has_raw_cols`` in that function), tagged with
the same ``synthetic:<type>`` / ``synthetic:context`` source. The EXISTS
clause recovers a batch-inferred injected row via that sibling row even after
``score_scale`` has been overwritten to ``"batch"``. It does NOT over-reach:
a genuine real-world detection has no ``synthetic:`` raw row at its exact
timestamp, so this clause cannot accidentally tag real data. Validated
against 9 row shapes on Postgres (see the Postgres verification table in the
backend-engineer pass-1 report).

``substr(..., 1, 64)``: both ``source`` and ``anomaly_type`` are
``String(64)``. ``'synthetic:' || anomaly_type`` can reach 74 characters for
a long ``anomaly_type``, which raises
``sqlstate=22001 StringDataRightTruncationError`` on Postgres at migration
time (reproduced). SQLite silently ignores the declared column width, which
is why this was not caught by the SQLite-only test suite before.

``SET LOCAL statement_timeout = 0``
------------------------------------
Applied (Postgres only — SQLite has no such statement and errors on it) as a
guard for environments whose role does not already disable the statement
timeout; the production role already reports ``statement_timeout: 0``, so
this changes nothing there and exists for other environments. No batching is
added for this UPDATE: migration 023's ``_BATCH_SIZE`` loop bounds Python
heap usage for a client-side row-by-row backfill loop (its own docstring),
which does not apply here — 025's backfill is a single server-side
``UPDATE ... WHERE`` with no Python-side iteration, so there is no client
memory to bound, and batching would only add complexity without shortening
the lock (one statement either way).

Known residual, deliberately not fixed here
---------------------------------------------
A row that was (1) injected before this fix existed, (2) into a tenant with
NO ``RawReading`` history at injection time (so ``has_raw_cols`` was false
and no sibling raw row was ever written), (3) later batch-inferred (so
``score_scale`` no longer reads ``'synthetic'``), all (4) before this
migration runs, is still missed by both the ``score_scale`` and ``EXISTS``
legs. Four preconditions must hold simultaneously; current production
exposure is 0 rows (verified by the backend-engineer pass-1 report).

Downgrade clears ``source`` wherever it was set by this migration (matches
``'synthetic:%'``), leaving ``'live'`` rows untouched.
"""

from __future__ import annotations

from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

# Mirrors the dialect-gating pattern used elsewhere in this migration chain
# (see alembic/versions/002_add_incident_memory_tables.py's _is_postgres()).
POSTGRES_DIALECT_NAME = "postgresql"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == POSTGRES_DIALECT_NAME


def upgrade() -> None:
    if _is_postgres():
        # SQLite has no SET LOCAL / statement_timeout — guard for other
        # environments; the production role already reports
        # statement_timeout: 0, so this is a no-op there. See module
        # docstring.
        op.execute("SET LOCAL statement_timeout = 0")

    op.execute(
        """
        UPDATE sensor_readings
           SET source = CASE
                 WHEN anomaly_type = 'synthetic_context' THEN 'synthetic:context'
                 WHEN anomaly_type IS NOT NULL
                   THEN substr('synthetic:' || anomaly_type, 1, 64)
                 ELSE 'synthetic:unknown'
               END
         WHERE source IS NULL
           AND (score_scale = 'synthetic' OR EXISTS (
                  SELECT 1 FROM raw_readings r
                   WHERE r.tenant_id = sensor_readings.tenant_id
                     AND r.greenhouse_id = sensor_readings.greenhouse_id
                     AND r.timestamp = sensor_readings.timestamp
                     AND r.source LIKE 'synthetic:%'
              ))
        """
    )


def downgrade() -> None:
    op.execute(
        "UPDATE sensor_readings SET source = NULL WHERE source LIKE 'synthetic:%'"
    )

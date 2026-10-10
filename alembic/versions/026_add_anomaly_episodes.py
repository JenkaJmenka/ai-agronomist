"""Add the anomaly_episodes table (general, detection-time fault windows).

Revision ID: 026
Revises: 025
Create Date: 2026-10-06

Background
----------
Today the only "episode" concept in the codebase is query-time: ``GET
/api/anomalies/episodes`` (``src/api/services/anomaly_episode_service.py``)
groups already-flagged ``sensor_readings`` rows by (channel, detector,
direction) on demand and persists nothing. This migration adds a *different*
``anomaly_episodes`` table: one row written *by a detector* at detection
time, one row per fault window, durable. The name collision is deliberate
(reviewed and accepted — see ``src/db/models.py``'s ``AnomalyEpisodeRecord``
docstring for the full distinction); this migration does not touch the
query-time path at all.

General by design: the stuck-fault detector is the first writer, but every
column name is detector-agnostic so band-breach and calibration-drift
detectors can write the same table later (Part B is tracked separately and
is NOT this migration — see "Known gap" below).

Timestamp contract (mirrors ``FaultWindow``,
``src/models/stuck_fault_detector.py:172``):
  - ``onset_ts``    — first sample inside the fault.
  - ``end_ts``      — FaultWindow.last_frozen_ts, the LAST FROZEN sample.
                       Known as soon as the fault is declared; updated as an
                       ongoing fault continues. NOT the release.
  - ``release_ts``  — the first NON-frozen sample after the freeze ends.
                       NULL while ``is_ongoing`` is True.
  - ``detected_ts`` — when the detector actually wrote the row (lags
                       ``onset_ts`` by whatever consecutive-sample threshold
                       the detector requires). ``detected_ts - onset_ts`` is
                       the detection latency; computed at query time only,
                       deliberately no column for it here.

``frozen_sample_count`` is counted over ``[onset_ts, end_ts]``: the finite
samples the detector actually saw stuck at the frozen value.

``raw_slot_count`` is NOT a tally of file rows. It is expected readings at
the channel's pinned cadence across the freeze:
  - raw_slot_count = expected readings at the pinned cadence across the
    freeze: (end_ts - onset_ts) / cadence + 1.
  - Cadence is pinned once: branch 1, the pre-onset median when at least
    CADENCE_PIN_MIN_INTERVALS intervals exist; otherwise branch 2, the
    median of the freeze's own intervals at gate fire. Never recomputed.
  - The gap (raw_slot_count - frozen_sample_count) counts readings missing
    FOR THIS CHANNEL, whatever the cause. That includes whole rows the
    adapter dropped because a different required sensor was null. It is
    NOT proof this sensor went offline.
  - ``CSVAdapter`` drops rows with a null required sensor before detection,
    which is why the definition is expected slots, not rows counted.

``duration_seconds`` is stored-and-derived from ``end_ts - onset_ts`` (the
frozen span observed so far), recomputed by the writer on every update — not
independently authoritative, and not ``release_ts - onset_ts`` (that longer
span is for alert suppression, not for describing freeze length).

``fault_type`` is NOT given a DB CHECK constraint here, unlike
``incidents.ck_incidents_fault_type`` (migrations 011/016/019). Mirroring it
would add a third hand-edited vocabulary location, and a migration must not
import the app-side list (see migration 025's docstring on why a migration
has to stay valid against the code as written). Left to application-layer
validation (not yet implemented — Part B owns it); see ``src/db/models.py``
for the full trade-off. A typo'd ``fault_type`` is accepted silently today.

``source`` mirrors ``SensorReading.source`` (ADR-010,
``docs/architecture/ADR-010-synthetic-provenance-marker.md``): NULL for a
real detection, ``'synthetic:<type>'`` for an inject-test row. Same
convention, same column name, deliberately not reinvented. This mirrors
migration 017 line 41 (``sa.Column("source", sa.String(64), nullable=True)``
on ``sensor_readings``), not migration 025 — 025 only backfills the
``'synthetic:<type>'`` convention into a column 017 already created; it adds
no DDL of its own.

Known gap (tracked separately, NOT this migration)
----------------------------------------------------
``FaultWindow`` (``src/models/stuck_fault_detector.py:172``) carries only
``channel``, ``onset_ts/dt``, ``release_ts/dt``, ``last_frozen_ts/dt``.
Neither ``frozen_sample_count`` nor ``raw_slot_count`` exists anywhere in the
detector today, and nothing INSERTs into this table at all. This migration
only creates the columns (nullable, so no writer is required to populate
them yet) and the constraints below; wiring the detector to compute and
persist the counts, performing the INSERT/UPDATE-on-conflict against the
upsert key (see "Constraints" below) at fault onset/continuation/release,
and implementing the application-layer ``fault_type`` validation this schema
omits, are all separate follow-ons (Part B; see
``docs/handoff/task3_4_spec.md``).

Compatibility
-------------
Works on both SQLite and PostgreSQL (follows migration 020's pattern):
  - sa.DateTime(timezone=True) -> TEXT on SQLite, TIMESTAMPTZ on PostgreSQL.
  - String/Integer/Float/Boolean map identically on both dialects.
  - render_as_batch=True is set in alembic/env.py (required for SQLite
    ALTER; not needed by this migration's create_table/create_index, but
    kept consistent with the rest of the chain for the downgrade path).
  - No server-side functions beyond the default no-op; no dialect branching
    needed anywhere in this migration (compare migration 025, which does
    need it for SET LOCAL statement_timeout).
  - The CHECK constraint is declared inline inside ``create_table`` (not via
    a separate ``op.create_check_constraint``/batch_alter_table like
    migrations 011/016/019 use against the pre-existing ``incidents``
    table): a CHECK present at table-creation time needs no ALTER on either
    dialect, so the batch-mode dance those migrations need for SQLite does
    not apply here.
  - The partial index below is the first use of ``postgresql_where`` /
    ``sqlite_where`` in this repo (``grep -rn "postgresql_where\\|sqlite_where"
    src alembic`` previously returned nothing). Both kwargs are required:
    passing only ``postgresql_where`` would silently make the SQLite index a
    full index, and since tests run on SQLite while prod runs Postgres, that
    gap would not surface in CI. SQLite has supported partial indexes since
    3.8.0, well below this project's floor.

Constraints
-----------
  - ``uq_anomaly_episodes_tenant_gh_channel_detector_onset`` — UNIQUE
    (tenant_id, greenhouse_id, channel, detector, onset_ts). This is the
    UPSERT KEY: it is the conflict target the Part B writer uses to update
    ``end_ts`` on an already-open fault instead of inserting a duplicate
    row, and it is what makes a re-upload of the same file idempotent
    (matching the same tenant/greenhouse/channel/detector/onset does not
    create a second episode).
  - ``ck_anomaly_episodes_release_ts_null_while_ongoing`` — CHECK (NOT
    is_ongoing OR release_ts IS NULL). Enforces ONE direction of the
    ``release_ts``/``is_ongoing`` invariant only: an ongoing fault cannot
    already have a release timestamp. The converse is deliberately NOT
    enforced — ``is_ongoing = false`` does NOT require ``release_ts IS NOT
    NULL``, because drift and band-breach episodes can close
    (``is_ongoing=False``) without ever observing a first normal sample to
    release against, unlike a stuck-fault freeze, which always has one.

Indexes
-------
  - ``ix_anomaly_episodes_tenant_gh_channel_onset``
    (tenant_id, greenhouse_id, channel, onset_ts) — the multi-tenant
    time-range lookup: "episodes for this tenant/greenhouse/channel in
    [start, end]". Mirrors ``ix_sensor_readings_tenant_gh_time``'s shape
    plus the channel column this table always filters on. Kept alongside
    the item-1 unique constraint rather than dropped as redundant: the
    unique constraint's column order is (..., channel, detector, onset_ts),
    so a query that ranges over ``onset_ts`` without also filtering on
    ``detector`` can only use the unique index's leftmost three columns
    (tenant_id, greenhouse_id, channel) and loses index ordering on
    onset_ts, since ``detector`` sits between ``channel`` and ``onset_ts``
    in that index. This index puts ``onset_ts`` immediately after
    ``channel`` so the common detector-agnostic time-range scan stays a
    single ordered index scan.
  - ``ix_anomaly_episodes_ongoing_tenant_gh`` — PARTIAL index on
    (tenant_id, greenhouse_id) WHERE is_ongoing, replacing the originally
    planned boolean-leading ``(is_ongoing, onset_ts)`` index. There is no
    reader of this table yet, so there is no query to justify a boolean-
    leading global index by; every read in this app filters by tenant_id,
    and a global ``(is_ongoing, onset_ts)`` index would be scanned across
    all tenants and would index the vast majority of rows (is_ongoing is
    false for every released episode) that "which episodes are open for
    this tenant/greenhouse" never wants. The partial predicate keeps the
    index small (only currently-open episodes) and the leading columns
    match the multi-tenant filter every query needs.
  - ``ix_anomaly_episodes_fault_type_detector`` (fault_type, detector) —
    reporting/filtering queries like "all stuck_fault episodes from the
    statistical detector".
  No standalone ``tenant_id`` index: the first composite index's leftmost
  prefix already covers tenant_id-only lookups.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None

_TABLE_NAME = "anomaly_episodes"

# NOT is_ongoing OR release_ts IS NULL: see "Constraints" in the module
# docstring for why only this direction is enforced.
_RELEASE_TS_CHECK_EXPR = "NOT is_ongoing OR release_ts IS NULL"

# "is_ongoing" alone is a valid boolean predicate on both dialects: Postgres
# evaluates the boolean column directly, SQLite treats its stored 0/1
# integer as falsy/truthy. See "Compatibility" in the module docstring for
# why both postgresql_where and sqlite_where must be set.
_ONGOING_PREDICATE = sa.text("is_ongoing")


def upgrade() -> None:
    op.create_table(
        _TABLE_NAME,
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("greenhouse_id", sa.String(255), nullable=False),
        sa.Column("channel", sa.String(255), nullable=False),
        sa.Column("detector", sa.String(64), nullable=False),
        sa.Column("fault_type", sa.String(64), nullable=False),
        sa.Column("onset_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("release_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("frozen_sample_count", sa.Integer(), nullable=True),
        sa.Column("raw_slot_count", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("trigger_value", sa.Float(), nullable=True),
        sa.Column(
            "is_ongoing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("source", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_anomaly_episodes"),
        sa.UniqueConstraint(
            "tenant_id",
            "greenhouse_id",
            "channel",
            "detector",
            "onset_ts",
            name="uq_anomaly_episodes_tenant_gh_channel_detector_onset",
        ),
        sa.CheckConstraint(
            _RELEASE_TS_CHECK_EXPR,
            name="ck_anomaly_episodes_release_ts_null_while_ongoing",
        ),
    )
    op.create_index(
        "ix_anomaly_episodes_tenant_gh_channel_onset",
        _TABLE_NAME,
        ["tenant_id", "greenhouse_id", "channel", "onset_ts"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_episodes_ongoing_tenant_gh",
        _TABLE_NAME,
        ["tenant_id", "greenhouse_id"],
        unique=False,
        postgresql_where=_ONGOING_PREDICATE,
        sqlite_where=_ONGOING_PREDICATE,
    )
    op.create_index(
        "ix_anomaly_episodes_fault_type_detector",
        _TABLE_NAME,
        ["fault_type", "detector"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_anomaly_episodes_fault_type_detector", table_name=_TABLE_NAME)
    op.drop_index("ix_anomaly_episodes_ongoing_tenant_gh", table_name=_TABLE_NAME)
    op.drop_index("ix_anomaly_episodes_tenant_gh_channel_onset", table_name=_TABLE_NAME)
    op.drop_table(_TABLE_NAME)

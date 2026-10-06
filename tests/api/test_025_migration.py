"""Tests for migration 025 — backfill the ``synthetic:<type>`` source marker.

Migration 025 moves the "this row is synthetic" signal off
``sensor_readings.anomaly_type`` and onto ``sensor_readings.source``, so that
real detection can start persisting ``anomaly_type`` without
``delete_synthetic_anomalies`` destroying genuine detections (see
``tests/api/test_synthetic_marker_contract.py``).

Rows injected before the marker existed carry ``source IS NULL``, which makes
them unreachable by the new ``source LIKE 'synthetic:%'`` delete predicate —
the backfill is what closes that gap, so it is the behaviour under test here.

This drives the real alembic chain (the ``test_023_resumable.py`` pattern)
rather than inlining the migration's SQL: a backfill is exactly the kind of
step where an inline copy can agree with itself while diverging from the
migration that actually runs.

SQLite limitation
-----------------
Runs against SQLite, so this covers the backfill's row classification but not
Postgres-specific DDL behaviour (lock duration, type affinity).  The
``source`` column already exists as of migration 017, so 025 should be
backfill-only with no DDL — if it grows an ALTER, the Postgres CI job
(``.github/workflows/postgres-tests.yml``) is what covers it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from tests.api._alembic_chain import alembic_config, ancestry_of, stamped_revision

_REVISION_BEFORE = "024"
_REVISION_UNDER_TEST = "025"

_SYNTHETIC_SPIKE_TYPE = "co2_surge"
_SYNTHETIC_FILLER_TYPE = "synthetic_context"
_LIVE_SOURCE = "live"

# Expected markers after upgrade, keyed by the seeded row's tenant-unique id.
_EXPECTED_MARKER_AFTER_UPGRADE = {
    "injected_spike": f"synthetic:{_SYNTHETIC_SPIKE_TYPE}",
    "injected_filler": "synthetic:context",
    "score_scale_only": "synthetic:unknown",
    "live": _LIVE_SOURCE,
}


def _seed_marker_cohorts(engine: sa.Engine) -> None:
    """Insert one row per cohort 025 must classify.

    ``greenhouse_id`` doubles as the row label so assertions can address rows
    without depending on autoincrement ids.  Timestamps differ per row because
    ``uq_sensor_readings_tenant_greenhouse_timestamp`` is in force from
    migration 013.
    """
    cohorts = [
        # label,              source,        anomaly_type,            score_scale
        ("live", _LIVE_SOURCE, None, "live"),
        ("injected_spike", None, _SYNTHETIC_SPIKE_TYPE, "synthetic"),
        ("injected_filler", None, _SYNTHETIC_FILLER_TYPE, "synthetic"),
        ("score_scale_only", None, None, "synthetic"),
    ]
    with engine.begin() as connection:
        for idx, (label, source, anomaly_type, score_scale) in enumerate(cohorts):
            connection.execute(
                text(
                    "INSERT INTO sensor_readings "
                    "(tenant_id, greenhouse_id, timestamp, temperature, humidity, "
                    " co2, light, anomaly_score, source, anomaly_type, score_scale) "
                    "VALUES ('t_marker', :label, :ts, 21.0, 60.0, 420.0, 500.0, "
                    "        0.5, :source, :anomaly_type, :score_scale)"
                ),
                {
                    "label": label,
                    "ts": f"2026-05-0{idx + 1}T00:00:00",
                    "source": source,
                    "anomaly_type": anomaly_type,
                    "score_scale": score_scale,
                },
            )


def _rows_by_label(engine: sa.Engine) -> dict[str, tuple[str | None, str | None]]:
    """Return ``{label: (source, anomaly_type)}`` for every seeded row."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT greenhouse_id, source, anomaly_type FROM sensor_readings "
                "WHERE tenant_id = 't_marker'"
            )
        ).fetchall()
    return {label: (source, anomaly_type) for label, source, anomaly_type in rows}


def _upgraded_engine(tmp_path, monkeypatch) -> tuple[sa.Engine, Config]:
    """Seed at 024 and upgrade to 025."""
    url = f"sqlite:///{tmp_path / 'marker.db'}"
    config = alembic_config(url, monkeypatch)
    command.upgrade(config, _REVISION_BEFORE)

    engine = sa.create_engine(url)
    _seed_marker_cohorts(engine)
    command.upgrade(config, _REVISION_UNDER_TEST)
    return engine, config


def test_025_marks_every_synthetic_row_with_a_source_prefix(tmp_path, monkeypatch):
    """Each synthetic cohort must come out carrying a ``synthetic:`` marker."""
    engine, _ = _upgraded_engine(tmp_path, monkeypatch)
    try:
        rows = _rows_by_label(engine)
        synthetic_labels = ("injected_spike", "injected_filler", "score_scale_only")
        unmarked = {
            label: rows[label][0]
            for label in synthetic_labels
            if not (rows[label][0] or "").startswith("synthetic:")
        }
        assert unmarked == {}, "every synthetic row must carry a synthetic: source"
    finally:
        engine.dispose()


def test_025_assigns_the_exact_marker_each_cohort_requires(tmp_path, monkeypatch):
    """Filler, spike, and score_scale-only rows get distinct markers.

    The distinction is load-bearing: type-scoped removal compares
    ``source == f"synthetic:{anomaly_type}"``, so collapsing filler into the
    spike marker would make "remove co2_surge" delete the background rows too.
    """
    engine, _ = _upgraded_engine(tmp_path, monkeypatch)
    try:
        rows = _rows_by_label(engine)
        assert rows["injected_spike"][0] == _EXPECTED_MARKER_AFTER_UPGRADE["injected_spike"]
        assert rows["injected_filler"][0] == _EXPECTED_MARKER_AFTER_UPGRADE["injected_filler"]
        assert rows["score_scale_only"][0] == _EXPECTED_MARKER_AFTER_UPGRADE["score_scale_only"]
    finally:
        engine.dispose()


def test_025_leaves_live_rows_completely_untouched(tmp_path, monkeypatch):
    """A live row must keep ``source='live'`` and a NULL ``anomaly_type``.

    Overwriting either would hand ``wipe_live_demo_data`` a row it can no
    longer reach through its primary ``source == 'live'`` clause.
    """
    engine, _ = _upgraded_engine(tmp_path, monkeypatch)
    try:
        assert _rows_by_label(engine)["live"] == (_LIVE_SOURCE, None)
    finally:
        engine.dispose()


def test_025_downgrade_clears_the_marker_without_disturbing_live(tmp_path, monkeypatch):
    """Downgrade restores ``source IS NULL`` for synthetic rows only."""
    engine, config = _upgraded_engine(tmp_path, monkeypatch)
    try:
        command.downgrade(config, _REVISION_BEFORE)

        rows = _rows_by_label(engine)
        still_marked = {
            label: rows[label][0]
            for label in ("injected_spike", "injected_filler", "score_scale_only")
            if rows[label][0] is not None
        }
        assert still_marked == {}, "downgrade must clear every synthetic: marker"
        assert rows["live"] == (
            _LIVE_SOURCE,
            None,
        ), "downgrade must not clear the live provenance tag"
    finally:
        engine.dispose()


def test_025_is_reachable_from_head(tmp_path, monkeypatch):
    """``upgrade head`` must apply 025 — a migration nobody reaches is inert.

    Asserted as membership in the chain alembic actually walked, not as
    ``stamped == "025"``: 025 stopped being head the moment 026 landed, and the
    equality form turned every later migration into a failure of this file.
    That the chain has one head and reaches it at all is a property of the
    chain, covered once in ``test_alembic_chain.py``; what stays meaningful
    here forever is that *this* revision is still on the applied path.
    """
    url = f"sqlite:///{tmp_path / 'head.db'}"
    config = alembic_config(url, monkeypatch)
    command.upgrade(config, "head")

    engine = sa.create_engine(url)
    try:
        landed_on = stamped_revision(engine)
        assert _REVISION_UNDER_TEST in ancestry_of(config, landed_on), (
            f"{_REVISION_UNDER_TEST} is not an ancestor of the revision "
            f"`upgrade head` landed on ({landed_on!r}) — the backfill tested "
            "above never runs"
        )
    finally:
        engine.dispose()

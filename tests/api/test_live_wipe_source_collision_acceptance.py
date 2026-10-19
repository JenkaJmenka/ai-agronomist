"""Acceptance: a CSV header must not be able to impersonate the live marker.

``RawReading.source`` carries provenance: ``"live"`` from
``src/live/persistence.py``, ``"synthetic:<...>"`` from
``src.config.synthetic_source``, and ``CSV_UPLOAD_RAW_SOURCE`` from a plain
upload.  Until the source-column-collision fix, a plain upload instead wrote
the CSV's *own* timestamp-column header string there, so a client-chosen
header could impersonate the live marker and ``DELETE /upload/live-data``
would delete that client's entire upload — from both ``raw_readings`` and
``sensor_readings``, because the raw rows it matched then supplied the
timestamps its (now deleted) ``source IS NULL`` fallback matched on.

Why the header here is ``live:timestamp`` and not a bare ``live``: see the
``COLLIDING_TS_HEADER`` comment in ``_live_wipe_fixtures.py``.  A bare ``live``
header is never read as a timestamp column at all, so it cannot reach
``source``, and a test written against it passes for the wrong reason.

The live direction is dramatized here because its blast radius was the larger
one — it reached ``sensor_readings`` too.  The ``synthetic:`` direction of the
same bug has its own, raw-only blast radius and its own acceptance file,
``test_synthetic_wipe_source_collision_acceptance.py``.

The no-regression half of this suite — that a real live scope is still wiped,
and that synthetic inject rows are still spared — lives in
``test_live_wipe_provenance_acceptance.py`` (300-line guardrail).

These tests drive the real routes against a real SQLite schema and assert on
surviving database rows.  Neither the upload route nor ``wipe_live_demo_data``
constructs an ``AnomalyDetector``, so nothing here relies on the
``patch_model_load`` autouse fixture in ``tests/api/conftest.py``
(BACKLOG-13); the suite passes identically with it inert.
"""

from __future__ import annotations

from src.config import CSV_UPLOAD_RAW_SOURCE
from tests.api._live_wipe_fixtures import (
    ROW_COUNT,
    colliding_csv_bytes,
    live_wipe_client,  # noqa: F401 — re-exported for pytest fixture discovery
    naive_upload_timestamps,
    raw_rows,
    raw_sources,
    run_live_wipe,
    sensor_rows,
    sensor_sources,
    upload_csv,
)


class TestCsvHeaderCannotImpersonateLiveSource:
    """A client-chosen CSV header must not make an upload wipeable as live data."""

    async def test_upload_under_a_live_prefixed_header_survives_a_live_wipe(
        self, live_wipe_client, db_session
    ):
        """The acceptance case: upload, wipe live data, upload intact in BOTH tables.

        Measured against the pre-fix modules (``csv_adapter.py`` and
        ``tenant_data_wipe.py`` as of ``39feecf``), this wipe returned
        ``deleted_raw=8, deleted_sensor=8`` and left both tables empty: all 8
        raw rows carried ``source='live:timestamp'`` and matched the
        ``LIKE 'live:%'`` arm, and those rows then supplied the timestamps the
        ``source IS NULL`` fallback used to match all 8 canonical rows.
        """
        assert upload_csv(live_wipe_client, colliding_csv_bytes())["uploaded"] == ROW_COUNT

        # The header really did drive the timestamp column — otherwise the rows
        # would have been dropped and "survives the wipe" would be vacuous.
        seeded_raw = await raw_rows(db_session)
        assert len(seeded_raw) == ROW_COUNT
        assert len(await sensor_rows(db_session)) == ROW_COUNT
        # SQLite-only comparison on purpose; see naive_upload_timestamps'
        # docstring for what breaks on Postgres and why it is not this
        # assertion's fault when it does.
        assert sorted(row.timestamp for row in seeded_raw) == naive_upload_timestamps()

        body = run_live_wipe(live_wipe_client)

        assert body["cleared_live"] is True
        assert body["deleted_raw"] == 0
        assert body["deleted_sensor"] == 0
        assert len(await raw_rows(db_session)) == ROW_COUNT, (
            "the upload's raw rows must survive a live wipe"
        )
        assert len(await sensor_rows(db_session)) == ROW_COUNT, (
            "the upload's canonical rows must survive a live wipe"
        )

    async def test_upload_stores_the_fixed_marker_not_the_colliding_header(
        self, live_wipe_client, db_session
    ):
        """``RawReading.source`` holds ``"csv"``, never the file's header string.

        The single assertion the collision reduces to.  Pre-fix this set was
        ``{'live:timestamp'}`` — both the header string and a match for the
        wipe's ``LIKE 'live:%'`` arm.
        """
        upload_csv(live_wipe_client, colliding_csv_bytes())

        assert await raw_sources(db_session) == {CSV_UPLOAD_RAW_SOURCE}

    async def test_upload_leaves_sensor_source_null(self, live_wipe_client, db_session):
        """A plain upload leaves ``SensorReading.source`` NULL (ADR-010).

        Pinned because it is the premise of the wipe change: NULL is a state
        real client data is routinely in, so the wipe may not treat it as a
        proxy for "this is a live row".

        Two assertions, not one: ``upload_csv`` asserts the upload returned
        HTTP 200 before this one looks at the stored rows.
        """
        upload_csv(live_wipe_client, colliding_csv_bytes())

        assert await sensor_sources(db_session) == {None}

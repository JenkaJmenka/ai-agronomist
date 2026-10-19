"""Acceptance: closing the collision must not blunt the live wipe.

The other half of the suite in
``test_live_wipe_source_collision_acceptance.py`` (split for the 300-line
guardrail).  That file pins what the wipe must now *spare*; this one pins what
it must still *remove*, and what it must continue to leave alone:

- a genuine live scope's rows go, through both ``source == 'live'`` and
  ``source LIKE 'live:%'``;
- synthetic inject rows stay — they belong to
  ``DELETE /upload/inject-test-anomalies``.

Shared constants, CSV builders, fixtures and survivor queries come from
``_live_wipe_fixtures.py``.
"""

from __future__ import annotations

from fastapi import status

from src.config import CSV_UPLOAD_RAW_SOURCE, synthetic_source
from tests.api._live_wipe_fixtures import (
    GREENHOUSE,
    LIVE_PREFIXED_SOURCE,
    LIVE_SOURCE,
    ROW_COUNT,
    STEP,
    T0,
    TENANT,
    add_one_live_sensor_row_and_one_live_raw_row,
    colliding_csv_bytes,
    live_wipe_client,  # noqa: F401 — re-exported for pytest fixture discovery
    plain_csv_bytes,
    raw_rows,
    raw_sources,
    run_live_wipe,
    sensor_rows,
    synthetic_raw_rows,
    synthetic_sensor_rows,
    upload_csv,
)

INJECTED_NORMAL = 3
INJECTED_ANOMALIES = 1
INJECTED_TOTAL = INJECTED_NORMAL + INJECTED_ANOMALIES
INJECTED_TYPE = "co2_surge"


def _inject_synthetic_rows(client) -> dict:
    response = client.post(
        "/api/upload/inject-test-anomalies",
        data={
            "tenant_id": TENANT,
            "greenhouse_id": GREENHOUSE,
            "n_normal": INJECTED_NORMAL,
            "n_anomalies": INJECTED_ANOMALIES,
            "anomaly_type": INJECTED_TYPE,
        },
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()


class TestLiveWipeStillRemovesRealLiveRows:
    """The wipe keeps working for the data it exists to clear."""

    async def test_removes_rows_of_a_real_live_scope_from_both_tables(
        self, live_wipe_client, db_session
    ):
        """Both raw arms fire: ``source == 'live'`` and ``source LIKE 'live:%'``.

        **The ``LIKE 'live:%'`` arm is retained defensively, not because any
        real data depends on it.**  ``LIVE_PREFIXED_SOURCE`` (``"live:mock"``)
        has no producer anywhere in the codebase: ``src/live/persistence.py``
        writes the bare literal ``"live"`` in both places it sets the column,
        and the CSV header path this suite exists to close was the only
        producer of ``'live:%'`` that has ever existed — so that arm was only
        ever matching the bug.  This test seeds the value by hand.

        Whoever later removes or repurposes that arm should read this test as
        coverage of a dead-but-declared contract, not as evidence that live
        data would break.  Deleting the ``LIKE`` arm and this seeded row
        together is a legitimate change; deleting the arm while leaving the
        test is what would look like a regression.

        Row counts: two calls to the seeding helper, each writing exactly one
        ``SensorReading`` (``source`` always ``'live'``) plus exactly one
        ``RawReading`` (``source`` as passed).  So the two raw rows split
        across the wipe's two raw arms, while both sensor rows hit its single
        exact-match clause.
        """
        add_one_live_sensor_row_and_one_live_raw_row(
            db_session, T0, raw_source=LIVE_SOURCE
        )
        add_one_live_sensor_row_and_one_live_raw_row(
            db_session, T0 + STEP, raw_source=LIVE_PREFIXED_SOURCE
        )
        await db_session.commit()
        assert len(await raw_rows(db_session)) == 2  # one per helper call
        assert len(await sensor_rows(db_session)) == 2  # one per helper call

        body = run_live_wipe(live_wipe_client)

        assert body["deleted_raw"] == 2, (
            "one raw row from the exact-match arm, one from the LIKE arm"
        )
        assert body["deleted_sensor"] == 2, (
            "both sensor rows carry source='live' regardless of raw_source"
        )
        assert await raw_rows(db_session) == []
        assert await sensor_rows(db_session) == []

    async def test_removes_live_rows_but_leaves_a_colliding_upload_alone(
        self, live_wipe_client, db_session
    ):
        """Live rows and a ``live:``-headed upload coexist; only the live rows go.

        Pre-fix this reported ``deleted_raw=9`` — the one live row plus all 8
        of the upload's, whose header had made them look live.
        """
        upload_csv(live_wipe_client, colliding_csv_bytes())
        add_one_live_sensor_row_and_one_live_raw_row(db_session, T0 - STEP)
        await db_session.commit()

        body = run_live_wipe(live_wipe_client)

        assert body["deleted_raw"] == 1
        assert body["deleted_sensor"] == 1
        assert await raw_sources(db_session) == {CSV_UPLOAD_RAW_SOURCE}
        assert len(await sensor_rows(db_session)) == ROW_COUNT


class TestLiveWipeLeavesSyntheticRows:
    """Inject-test rows belong to ``DELETE /upload/inject-test-anomalies``."""

    async def test_synthetic_rows_survive_a_live_wipe_in_both_tables(
        self, live_wipe_client, db_session
    ):
        """``synthetic:%`` rows are untouched while the live rows beside them go.

        The CSV upload comes first for a reason beyond realism:
        ``_resolve_sample_stats`` learns its column ranges from the tenant's
        existing ``RawReading`` rows and sets ``has_raw_cols=False`` when there
        are none, in which case ``inject_synthetic_anomalies`` writes *no*
        sibling raw rows at all — and "synthetic raw rows survive" asserted
        against zero of them would pass vacuously.
        """
        upload_csv(live_wipe_client, plain_csv_bytes())
        assert _inject_synthetic_rows(live_wipe_client)["injected"] == INJECTED_TOTAL
        assert len(await synthetic_raw_rows(db_session)) == INJECTED_TOTAL
        assert len(await synthetic_sensor_rows(db_session)) == INJECTED_TOTAL

        add_one_live_sensor_row_and_one_live_raw_row(db_session, T0 - STEP)
        await db_session.commit()

        body = run_live_wipe(live_wipe_client)

        assert body["deleted_raw"] == 1
        assert body["deleted_sensor"] == 1
        assert len(await synthetic_raw_rows(db_session)) == INJECTED_TOTAL, (
            "a live wipe must not touch synthetic raw rows"
        )
        assert len(await synthetic_sensor_rows(db_session)) == INJECTED_TOTAL, (
            "a live wipe must not touch synthetic canonical rows"
        )
        assert await raw_sources(db_session) <= {
            CSV_UPLOAD_RAW_SOURCE,
            synthetic_source(INJECTED_TYPE),
            synthetic_source(None),
        }, "only upload and synthetic provenance may remain"

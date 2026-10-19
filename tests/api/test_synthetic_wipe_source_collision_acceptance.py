"""Acceptance: a CSV header must not impersonate the synthetic marker either.

The second direction of the source-column collision.  The live direction, and
the background on why a colliding header reaches ``RawReading.source`` at all,
are in ``test_live_wipe_source_collision_acceptance.py``; this file covers the
other reserved prefix, which has its own route and its own blast radius:

- ``DELETE /upload/live-data`` matches ``RawReading.source == 'live'`` or
  ``LIKE 'live:%'``, and pre-fix its ``source IS NULL`` timestamp fallback
  then cascaded into ``sensor_readings`` — so the live direction destroyed an
  upload in **both** tables;
- ``DELETE /upload/inject-test-anomalies`` matches
  ``RawReading.source LIKE 'synthetic:%'``
  (``src/api/services/synthetic_anomalies.py``) with no such fallback, and its
  ``SensorReading`` clause is ``source LIKE 'synthetic:%'`` — which an upload's
  rows never match, because an upload leaves that column NULL (ADR-010).  So
  this direction silently deleted an upload's **raw** rows only, leaving the
  canonical rows behind with no full-fidelity records under them.

``tests/unit/test_csv_adapter_coverage.py`` already pins the adapter-level
invariant (``source`` no longer varies with the header at all).  That is the
unit-level cause; these tests pin the route-level effect, which is what the
blast radius above actually consists of and what a future change to either
wipe would be measured against.

Shared constants, the CSV builder and the survivor queries come from
``_live_wipe_fixtures.py``.
"""

from __future__ import annotations

from fastapi import status
from fastapi.testclient import TestClient

from src.config import CSV_UPLOAD_RAW_SOURCE
from tests.api._live_wipe_fixtures import (
    GREENHOUSE,
    ROW_COUNT,
    TENANT,
    live_wipe_client,  # noqa: F401 — re-exported for pytest fixture discovery
    raw_rows,
    raw_sources,
    sensor_rows,
    sensor_sources,
    synthetic_colliding_csv_bytes,
    upload_csv,
)


def run_synthetic_wipe(client: TestClient) -> dict:
    """Drive ``DELETE /api/upload/inject-test-anomalies``, assert 200, return body.

    No ``anomaly_type`` is sent, which is the broad form: the service then
    matches the whole ``synthetic:%`` prefix rather than one exact marker, and
    that prefix form is the one a colliding CSV header could satisfy.
    """
    response = client.delete(
        "/api/upload/inject-test-anomalies",
        params={"tenant_id": TENANT, "greenhouse_id": GREENHOUSE},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()


class TestCsvHeaderCannotImpersonateSyntheticSource:
    """A client-chosen CSV header must not make an upload wipeable as inject data."""

    async def test_upload_under_a_synthetic_prefixed_header_survives_a_synthetic_wipe(
        self, live_wipe_client, db_session
    ):
        """Upload, remove all synthetic anomalies, upload intact in both tables.

        Pre-fix all 8 raw rows carried ``source='synthetic:timestamp'`` and
        matched the service's ``LIKE 'synthetic:%'`` raw clause, so this wipe
        returned ``deleted_raw=8`` and emptied ``raw_readings`` while the 8
        canonical rows stayed — an upload left without the full-fidelity rows
        the monitoring read path and ``_resolve_sample_stats`` both depend on.
        """
        assert (
            upload_csv(live_wipe_client, synthetic_colliding_csv_bytes())["uploaded"]
            == ROW_COUNT
        )
        assert len(await raw_rows(db_session)) == ROW_COUNT
        assert len(await sensor_rows(db_session)) == ROW_COUNT

        body = run_synthetic_wipe(live_wipe_client)

        assert body["deleted_raw"] == 0, (
            "no row of a plain upload may match the synthetic prefix"
        )
        assert body["deleted_sensor"] == 0
        assert len(await raw_rows(db_session)) == ROW_COUNT, (
            "the upload's raw rows must survive a synthetic wipe"
        )
        assert len(await sensor_rows(db_session)) == ROW_COUNT, (
            "the upload's canonical rows must survive a synthetic wipe"
        )

    async def test_upload_stores_the_fixed_marker_not_the_synthetic_header(
        self, live_wipe_client, db_session
    ):
        """``RawReading.source`` holds ``"csv"``, never ``"synthetic:timestamp"``.

        The assertion this direction of the collision reduces to, and the
        reason the wipe above deletes nothing.
        """
        upload_csv(live_wipe_client, synthetic_colliding_csv_bytes())

        assert await raw_sources(db_session) == {CSV_UPLOAD_RAW_SOURCE}
        assert await sensor_sources(db_session) == {None}

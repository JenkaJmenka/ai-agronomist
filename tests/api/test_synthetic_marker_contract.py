"""Contract tests for the ``source='synthetic:<type>'`` provenance marker.

Before this change ``sensor_readings.anomaly_type`` had exactly one writer of a
non-NULL value — ``src/api/services/synthetic_anomalies.py`` (the inject-test
endpoint) — so ``anomaly_type IS NOT NULL`` doubled as a de-facto "this row is
synthetic" marker.  Two call sites baked that coincidence in:

- ``delete_synthetic_anomalies`` keyed "remove all synthetic" off
  ``anomaly_type.is_not(None)``.
- ``wipe_live_demo_data`` (``tenant_data_wipe.py:209-213``) uses
  ``anomaly_type.is_(None)`` as an inverse guard in its *fallback* branch.

Once real detection persists ``anomaly_type`` (upcoming work), the first
destroys genuine detections and the second leaks live rows.  The approved fix
marks synthetic rows with ``SensorReading.source = "synthetic:<type>"``
(``"synthetic:context"`` for filler), mirroring the ``RawReading`` convention
already in place at ``synthetic_anomalies.py:304``.

These tests pin the *post-fix* contract.  The two
``test_should_keep_a_real_detection_when_removing_*`` tests below failed
against the pre-marker code by design and pass now that removal keys off
``source``; ``test_should_not_delete_a_legacy_unbackfilled_synthetic_row``
pins the resulting dependency on the migration 025 backfill.  See
``docs/coverage/synthetic_marker_phase2_qa.md``.  The live-wipe half of the
contract — including ``test_should_wipe_a_pre_tag_live_row_carrying_an_anomaly_type``
— lives in ``test_synthetic_marker_contract_live_wipe.py``.

Why the synthetic ``SensorReading`` rows here carry a ``source`` value
---------------------------------------------------------------------
Post-fix, inject writes the marker itself, and migration 025 backfills rows
injected before it.  A synthetic row with ``source IS NULL`` is therefore not
a state the fixed system can produce — except on a database that skipped the
backfill, which ``test_should_not_delete_a_legacy_unbackfilled_synthetic_row``
pins explicitly as a known migration dependency rather than leaving implicit.

Shared row builders, survivor queries and tenant constants come from
``tests/api/_marker_fixtures.py``; ``db_session`` from ``tests/api/conftest.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.synthetic_anomalies import delete_synthetic_anomalies
from tests.api._marker_fixtures import (
    GREENHOUSE,
    LIVE_SOURCE,
    REAL_DETECTOR_LABEL,
    SYNTHETIC_MARKER,
    SYNTHETIC_TYPE,
    TENANT,
    raw_row,
    sensor_row,
    surviving_raw_sources,
    surviving_sensor_rows,
)

# ---------------------------------------------------------------------------
# Test 1 — "Remove all synthetic" must not delete real detections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_keep_a_real_detection_when_removing_all_synthetic(
    db_session: AsyncSession,
) -> None:
    """A detector-labelled live row must survive "remove all synthetic".

    FAILED pre-fix: ``delete_synthetic_anomalies`` keyed off
    ``anomaly_type.is_not(None)``, which matches ``UNDER_HEATING`` just as
    readily as ``co2_surge``, so the genuine detection was destroyed along with
    the injected one.
    """
    db_session.add_all(
        [
            sensor_row(
                index=0,
                anomaly_type=REAL_DETECTOR_LABEL,
                source=LIVE_SOURCE,
                anomaly_score=0.83,
                score_scale="live",
            ),
            sensor_row(
                index=1,
                anomaly_type=SYNTHETIC_TYPE,
                source=SYNTHETIC_MARKER,
                anomaly_score=0.91,
                score_scale="synthetic",
            ),
            raw_row(index=1, source=SYNTHETIC_MARKER),
        ]
    )
    await db_session.commit()

    result = await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
    )

    assert await surviving_sensor_rows(db_session) == {
        (LIVE_SOURCE, REAL_DETECTOR_LABEL)
    }, "the real detection must survive; only the synthetic:-marked row is removable"
    assert result["deleted_sensor"] == 1
    assert await surviving_raw_sources(db_session) == []


@pytest.mark.asyncio
async def test_should_keep_a_real_detection_when_removing_one_synthetic_type(
    db_session: AsyncSession,
) -> None:
    """Type-scoped removal must not collide with a same-named detector label.

    The type-scoped branch used to compare ``anomaly_type == resolved_type``,
    so a detector that ever emits a label matching a member of
    ``ANOMALY_TYPES`` would have its rows deleted.  Keying off
    ``source == 'synthetic:<type>'`` removes that whole class of collision.
    """
    db_session.add_all(
        [
            sensor_row(
                index=0,
                anomaly_type=SYNTHETIC_TYPE,
                source=LIVE_SOURCE,
                anomaly_score=0.88,
                score_scale="live",
            ),
            sensor_row(
                index=1,
                anomaly_type=SYNTHETIC_TYPE,
                source=SYNTHETIC_MARKER,
                anomaly_score=0.91,
                score_scale="synthetic",
            ),
        ]
    )
    await db_session.commit()

    result = await delete_synthetic_anomalies(
        db_session,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        anomaly_type=SYNTHETIC_TYPE,
    )

    assert await surviving_sensor_rows(db_session) == {(LIVE_SOURCE, SYNTHETIC_TYPE)}
    assert result["deleted_sensor"] == 1


@pytest.mark.asyncio
async def test_should_not_delete_a_legacy_unbackfilled_synthetic_row(
    db_session: AsyncSession,
) -> None:
    """Documents the fix's dependency on the migration 025 backfill.

    Once deletion keys off ``source``, a synthetic row written *before* the
    marker existed (``source IS NULL``) is unreachable by "remove all
    synthetic".  Migration 025 is what makes that state unreachable in
    practice; this test exists so the coupling is visible in the suite rather
    than discovered as a support ticket.  It is a statement of the trade-off,
    not a bug report.
    """
    db_session.add(
        sensor_row(
            index=0,
            anomaly_type=SYNTHETIC_TYPE,
            source=None,
            anomaly_score=0.91,
            score_scale="synthetic",
        )
    )
    await db_session.commit()

    await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
    )

    assert await surviving_sensor_rows(db_session) == {(None, SYNTHETIC_TYPE)}

"""Acceptance tests for "real data wins" when an upload lands on a synthetic row.

The defect (N1).  ``inject_synthetic_anomalies`` tags its rows
``source='synthetic:<type>'`` (ADR-010).  A CSV upload covering the same
timestamps upserts with ``update_cols=["temperature", "humidity", "co2",
"light"]`` (``src/api/routes/upload.py:121``), and ``bulk_upsert_rows`` builds
its ``ON CONFLICT DO UPDATE`` set-map from exactly those columns
(``src/db/upsert.py:74-81``) — ``source`` and ``anomaly_type`` are not among
them.  The row therefore keeps a synthetic marker while holding REAL measured
values, and the next "remove all synthetic" deletes the operator's real data.

The approved rule: **real data wins.**  When an upload upsert lands on an
existing row whose ``source LIKE 'synthetic:%'``, that row becomes real —
``source`` and ``anomaly_type`` are cleared and the synthetic scoring columns
are reset.

These tests drive the production upsert helper with the same arguments
``upload.py`` passes, so they pin production behaviour rather than a
hand-written UPDATE.  The fix must therefore live where that call can reach it
(``bulk_upsert_rows`` or something it calls) — a fix applied only in
``upload.py`` around the call would leave these red.

The converse half of the rule — a non-synthetic row must not become collateral
damage — lives in ``test_upload_overwrites_non_synthetic_rows.py`` (300-line
guardrail).  Row builders, the upload upsert and tenant constants come from
``tests/api/_marker_fixtures.py``; ``db_session`` from ``tests/api/conftest.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.synthetic_anomalies import (
    delete_synthetic_anomalies,
    inject_synthetic_anomalies,
)
from src.db.models import SensorReading
from tests.api._marker_fixtures import (
    REAL_VALUES,
    SYNTHETIC_MARKER,
    SYNTHETIC_TYPE,
    UPLOAD_GREENHOUSE,
    UPLOAD_T,
    UPLOAD_TENANT,
    measured_values_of,
    only_upload_row,
    upload_collision_row,
    upload_upsert,
)


# ---------------------------------------------------------------------------
# The rule: an upload that overwrites a synthetic row makes it real
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_clear_the_synthetic_marker_when_an_upload_overwrites_the_row(
    db_session: AsyncSession,
) -> None:
    """FAILS pre-fix: the row keeps ``source='synthetic:co2_surge'``.

    ``update_cols`` covers only the four sensor floats, so the upsert replaces
    the fabricated values with the operator's measured ones and leaves the
    provenance marker and the injected label in place.
    """
    db_session.add(upload_collision_row())
    await db_session.commit()

    await upload_upsert(db_session, UPLOAD_T)
    db_session.expire_all()

    row = await only_upload_row(db_session)
    assert measured_values_of(row) == REAL_VALUES, "the upload's real values must land"
    assert row.source is None, "an overwritten row is real data; the synthetic marker must go"
    assert row.anomaly_type is None, "the injected label must not outlive the fabricated values"


@pytest.mark.asyncio
async def test_should_keep_real_values_through_remove_all_synthetic_after_an_upload(
    db_session: AsyncSession,
) -> None:
    """FAILS pre-fix: "remove all synthetic" deletes the operator's real data.

    The end-to-end consequence of the marker outliving the values — this is
    the data-loss path, stated as the user sees it.
    """
    db_session.add(upload_collision_row())
    await db_session.commit()
    await upload_upsert(db_session, UPLOAD_T)

    result = await delete_synthetic_anomalies(
        db_session,
        tenant_id=UPLOAD_TENANT,
        greenhouse_id=UPLOAD_GREENHOUSE,
        anomaly_type=None,
    )
    db_session.expire_all()

    assert result["deleted_sensor"] == 0, "there is no synthetic row left to delete"
    row = await only_upload_row(db_session)
    assert measured_values_of(row) == REAL_VALUES, "real measured data must survive the wipe"


@pytest.mark.asyncio
async def test_should_reset_synthetic_scoring_columns_when_an_upload_overwrites_the_row(
    db_session: AsyncSession,
) -> None:
    """FAILS pre-fix: ``score_scale`` stays ``'synthetic'`` over real values.

    Asserts only that the row no longer claims the synthetic scale — the exact
    reset value (NULL, or a recomputed live score) is backend's call, so this
    test does not pin it.
    """
    db_session.add(upload_collision_row())
    await db_session.commit()

    await upload_upsert(db_session, UPLOAD_T)
    db_session.expire_all()

    row = await only_upload_row(db_session)
    assert row.score_scale != "synthetic", (
        "a synthetic score band must not be reported for real measured values"
    )


@pytest.mark.asyncio
async def test_should_clear_the_marker_when_an_upload_overwrites_a_really_injected_row(
    db_session: AsyncSession,
) -> None:
    """The same rule, reached the way a user reaches it: inject, then upload.

    FAILS pre-fix.  The tests above seed the post-inject row state directly;
    this one proves the collision is reachable from real injected timestamps —
    the upsert lands on the injected row (row count stays at the injected 20
    rather than growing to 21) and rewrites its values while leaving the
    marker.
    """
    injected = await inject_synthetic_anomalies(
        db_session,
        tenant_id=UPLOAD_TENANT,
        greenhouse_id=UPLOAD_GREENHOUSE,
        n_normal=10,
        n_anomalies=10,
        anomaly_type=SYNTHETIC_TYPE,
        window_start="2026-04-05T00:00:00+00:00",
        window_end="2026-04-07T00:00:00+00:00",
    )
    spike = (
        await db_session.execute(
            select(SensorReading).where(SensorReading.source == SYNTHETIC_MARKER)
        )
    ).scalars().first()
    assert spike is not None, "guard: the injection must have produced a marked spike row"

    await upload_upsert(db_session, spike.timestamp)
    db_session.expire_all()

    rows = (
        await db_session.execute(
            select(SensorReading).where(SensorReading.tenant_id == UPLOAD_TENANT)
        )
    ).scalars().all()
    assert len(rows) == injected["injected"], "the upload must update in place, not insert"

    overwritten = next(row for row in rows if row.timestamp == spike.timestamp)
    assert measured_values_of(overwritten) == REAL_VALUES
    assert overwritten.source is None
    assert overwritten.anomaly_type is None

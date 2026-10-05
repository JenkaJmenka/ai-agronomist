"""Contract tests for ``wipe_live_demo_data`` and the synthetic marker.

Split out of ``test_synthetic_marker_contract.py`` (300-line guardrail) — see
that module's docstring for the full provenance-marker background. This file
covers Test 2 from that contract: the live wipe must not leak rows that gain
an ``anomaly_type``, and must not regress once it keys off ``source`` instead.

Row builders, survivor queries and tenant constants come from
``tests/api/_marker_fixtures.py`` — a helpers module both files import
explicitly, so neither reaches into the other's internals.  ``db_session``
comes from ``tests/api/conftest.py``, where pytest discovers it without an
import.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.tenant_data_wipe import wipe_live_demo_data
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


class _FakeLiveRuntime:
    """Stand-in for ``LiveRuntime``.

    ``_suppress_live_persistence`` (``tenant_data_wipe.py:42-66``) swallows any
    exception from ``suppress_persistence`` and logs it, so a runtime missing
    the method would still let the wipe run — but with a misleading
    ``AttributeError`` traceback in the captured log.  Recording the calls
    keeps that path honest and the output clean.
    """

    def __init__(self) -> None:
        self.suppressed: list[tuple[str, str]] = []

    def suppress_persistence(self, tenant_id: str, greenhouse_id: str) -> None:
        self.suppressed.append((tenant_id, greenhouse_id))


# ---------------------------------------------------------------------------
# Test 2 — the live wipe must not leak rows that gain an anomaly_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_wipe_a_source_tagged_live_row_carrying_an_anomaly_type(
    db_session: AsyncSession,
) -> None:
    """Regression guard — PASSES pre-fix, and must keep passing.

    ``wipe_live_demo_data``'s *primary* clause is
    ``SensorReading.source == 'live'`` (``tenant_data_wipe.py:204``), which
    matches irrespective of ``anomaly_type``.  The ``anomaly_type IS NULL``
    guard lives only in the fallback branch, so a properly source-tagged live
    row that gains a detector label is already wiped correctly today.  Pinned
    here so the fix to the fallback branch cannot regress the primary one.
    """
    live_runtime = _FakeLiveRuntime()
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
        ]
    )
    await db_session.commit()

    result = await wipe_live_demo_data(db_session, live_runtime, TENANT, GREENHOUSE)

    assert await surviving_sensor_rows(db_session) == {
        (SYNTHETIC_MARKER, SYNTHETIC_TYPE)
    }, "the live row must be wiped and the injected row left alone"
    assert result["deleted_sensor"] == 1
    assert live_runtime.suppressed == [(TENANT, GREENHOUSE)]


@pytest.mark.asyncio
async def test_should_wipe_a_pre_tag_live_row_carrying_an_anomaly_type(
    db_session: AsyncSession,
) -> None:
    """A pre-``source``-tag live row must still be wiped once it gains a label.

    FAILS pre-fix.  The fallback branch (``tenant_data_wipe.py:209-213``)
    reaches rows with ``source IS NULL`` that share a timestamp with a live
    ``RawReading``, but additionally requires ``anomaly_type IS NULL`` as a
    proxy for "not an inject row".  A pre-tag live row that gains a detector
    label fails that guard and leaks: the wipe reports success while the row
    stays in the table.

    The fallback only engages when ``live_timestamps`` is non-empty, hence the
    live ``RawReading`` at the same timestamp.
    """
    live_runtime = _FakeLiveRuntime()
    db_session.add_all(
        [
            sensor_row(
                index=0,
                anomaly_type=REAL_DETECTOR_LABEL,
                source=None,
                anomaly_score=0.83,
                score_scale="live",
            ),
            raw_row(index=0, source=LIVE_SOURCE),
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

    result = await wipe_live_demo_data(db_session, live_runtime, TENANT, GREENHOUSE)

    assert await surviving_sensor_rows(db_session) == {
        (SYNTHETIC_MARKER, SYNTHETIC_TYPE)
    }, "the pre-tag live row must not survive the live wipe"
    assert result["deleted_sensor"] == 1
    assert await surviving_raw_sources(db_session) == [SYNTHETIC_MARKER]

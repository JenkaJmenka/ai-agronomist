"""Contract tests for ``wipe_live_demo_data`` and the synthetic marker.

Split out of ``test_synthetic_marker_contract.py`` (300-line guardrail) — see
that module's docstring for the full provenance-marker background. This file
covers Test 2 from that contract: the live wipe keys off ``source`` alone, so
it must wipe a ``source='live'`` row whatever ``anomaly_type`` that row has
gained, and must leave every row it cannot positively identify as live.

The second half of that contract inverted with the source-column-collision
fix. ``wipe_live_demo_data`` used to fall back to matching ``source IS NULL``
rows that shared a timestamp with a live ``RawReading``, as a proxy for
"pre-tag live row"; the fallback is now deleted. ``source IS NULL`` is also
the exact state a plain CSV upload leaves ``SensorReading`` rows in (ADR-010),
so that proxy deleted real client uploads whenever their timestamps coincided
with any live row in the same scope. An untagged row therefore now *survives*
a live wipe — see the second test below, which asserted the opposite before.

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

    ``_suppress_live_persistence`` (``tenant_data_wipe.py:78-105``) swallows any
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
    """Regression guard — passed before the collision fix, and must keep passing.

    ``wipe_live_demo_data``'s only ``SensorReading`` clause is
    ``SensorReading.source == 'live'``, which matches irrespective of
    ``anomaly_type``: a properly source-tagged live row that later gains a
    detector label is still wiped.  Pinned here so that deleting the
    ``source IS NULL`` timestamp fallback — the subject of the next test —
    could not quietly narrow this clause too.
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
async def test_should_keep_an_untagged_row_sharing_a_timestamp_with_a_live_row(
    db_session: AsyncSession,
) -> None:
    """An untagged ``SensorReading`` survives a live wipe, whatever its timestamp.

    **This assertion is inverted from what it was.**  It used to require that
    an untagged row (``source IS NULL``) sharing a timestamp with a live
    ``RawReading`` be *wiped*, via ``wipe_live_demo_data``'s timestamp
    fallback.  That fallback is deleted, because ``source IS NULL`` is not a
    live-row proxy: it is equally the state a plain CSV upload leaves its
    ``SensorReading`` rows in (ADR-010 keeps ``source`` NULL for an upload),
    so the fallback deleted real client data whose timestamps happened to
    coincide with a live row in the same scope.

    Both untagged rows below are seeded at timestamps that a live
    ``RawReading`` also occupies — the precise condition that used to doom
    them — and both must come through.  One carries a detector label and one
    does not, so neither an ``anomaly_type IS NULL`` guard nor its negation
    can resurrect the fallback unnoticed.

    The live ``RawReading`` rows themselves are still removed: ``RawReading``
    is matched on ``source`` directly, and that side of the wipe is unchanged.
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
            sensor_row(
                index=2,
                anomaly_type=None,
                source=None,
                anomaly_score=0.04,
            ),
            raw_row(index=2, source=LIVE_SOURCE),
        ]
    )
    await db_session.commit()

    result = await wipe_live_demo_data(db_session, live_runtime, TENANT, GREENHOUSE)

    assert await surviving_sensor_rows(db_session) == {
        (None, REAL_DETECTOR_LABEL),
        (None, None),
        (SYNTHETIC_MARKER, SYNTHETIC_TYPE),
    }, "an untagged row is indistinguishable from a CSV upload and must survive"
    assert result["deleted_sensor"] == 0
    assert await surviving_raw_sources(db_session) == [SYNTHETIC_MARKER]
    assert result["deleted_raw"] == 2

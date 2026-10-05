"""Behavioural tests for synthetic anomaly *removal*.

Split out of ``test_synthetic_anomalies_service.py`` (300-line guardrail),
which keeps the injection half.  Covers ``delete_synthetic_anomalies``:
request validation, type scoping, tenant scoping, and the guarantee that real
sensor data survives.

These tests build their rows by hand to describe states removal must handle;
the round trip against a *real* injection — the one test that must not depend
on a test-side copy of the marker format — lives with the writer in
``test_synthetic_anomalies_service.py``.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.synthetic_anomalies import delete_synthetic_anomalies
from tests.api._synthetic_service_fixtures import (
    GREENHOUSE,
    HISTORY_ROWS,
    OTHER_TENANT,
    TENANT,
    count_sensor_rows,
    detector_labelled_sensor_row,
    raw_sources,
    seed_raw_history,
    synthetic_raw_row,
    synthetic_sensor_row,
)


@pytest.mark.asyncio
async def test_should_reject_unknown_anomaly_type_on_delete(db_session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await delete_synthetic_anomalies(
            db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type="tornado"
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_should_delete_only_the_requested_anomaly_type(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            synthetic_sensor_row(anomaly_type="co2_surge", index=0),
            synthetic_sensor_row(anomaly_type="humidity_spike", index=1),
            synthetic_raw_row(source="synthetic:co2_surge", index=0),
            synthetic_raw_row(source="synthetic:humidity_spike", index=1),
        ]
    )
    await db_session.commit()

    result = await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type="co2_surge"
    )

    assert result["deleted_sensor"] == 1
    assert result["deleted_raw"] == 1
    assert await raw_sources(db_session) == ["synthetic:humidity_spike"]


@pytest.mark.asyncio
@pytest.mark.parametrize("all_types_argument", [None, ""], ids=["omitted", "empty"])
async def test_should_treat_blank_anomaly_type_as_remove_all_synthetic(
    db_session: AsyncSession, all_types_argument: str | None
) -> None:
    db_session.add_all(
        [
            synthetic_sensor_row(anomaly_type="co2_surge", index=0),
            synthetic_sensor_row(anomaly_type="synthetic_context", index=1),
            synthetic_raw_row(source="synthetic:co2_surge", index=0),
            synthetic_raw_row(source="synthetic:context", index=1),
        ]
    )
    await db_session.commit()

    result = await delete_synthetic_anomalies(
        db_session,
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        anomaly_type=all_types_argument,
    )

    assert result["anomaly_type"] == "all"
    assert result["deleted_sensor"] == 2
    assert result["deleted_raw"] == 2


@pytest.mark.asyncio
async def test_should_treat_whitespace_only_anomaly_type_as_remove_all_synthetic(
    db_session: AsyncSession,
) -> None:
    """Whitespace-only input means "all", like the empty string and omission.

    Was an xfail(strict) bug: ``delete_synthetic_anomalies`` stripped the value
    and then rejected the resulting empty string, so ``'   '`` raised
    "Unknown anomaly_type ''" — even though the ``strip()`` exists precisely to
    normalise that input (see the "Treat empty string the same as omit" comment
    above it).
    """
    db_session.add(synthetic_sensor_row(anomaly_type="co2_surge", index=0))
    await db_session.commit()

    result = await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type="   "
    )

    assert result["anomaly_type"] == "all"
    assert result["deleted_sensor"] == 1


@pytest.mark.asyncio
async def test_should_never_delete_real_sensor_data(db_session: AsyncSession) -> None:
    """Untagged canonical rows and non-synthetic raw sources must survive."""
    await seed_raw_history(db_session)
    db_session.add(synthetic_sensor_row(anomaly_type=None, index=99))
    db_session.add(synthetic_sensor_row(anomaly_type="co2_surge", index=98))
    await db_session.commit()

    await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
    )

    assert await count_sensor_rows(db_session) == 1, "the untagged real row must survive"
    assert await raw_sources(db_session) == ["csv"] * HISTORY_ROWS


@pytest.mark.asyncio
async def test_should_leave_other_tenants_synthetic_rows_untouched(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            synthetic_sensor_row(anomaly_type="co2_surge", index=0),
            synthetic_sensor_row(anomaly_type="co2_surge", index=0, tenant_id=OTHER_TENANT),
            synthetic_raw_row(source="synthetic:co2_surge", index=0),
            synthetic_raw_row(source="synthetic:co2_surge", index=0, tenant_id=OTHER_TENANT),
        ]
    )
    await db_session.commit()

    await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
    )

    assert await count_sensor_rows(db_session, OTHER_TENANT) == 1
    assert await raw_sources(db_session, OTHER_TENANT) == ["synthetic:co2_surge"]


@pytest.mark.asyncio
async def test_should_key_removal_off_the_source_marker_not_the_anomaly_type_label(
    db_session: AsyncSession,
) -> None:
    """"Remove all synthetic" keys off the ``synthetic:`` source marker.

    History — this assertion used to run inverted, pinning the old behaviour
    that removal matched *any* non-null ``anomaly_type``, with the note:

        ``src/api/services/fault_classification.py:73-76`` documents two
        further writers of that column (setpoint_scada_detector,
        band_breach_detector, both UPPER_CASE labels).  Should either ever
        persist to ``SensorReading``, this assertion flips from "pinned
        behaviour" to "detector-labelled real rows are silently destroyed".

    That prediction is what this whole change answers: detection is about to
    start persisting ``anomaly_type``, so the label can no longer stand in for
    provenance.  A detector-labelled row now SURVIVES unless it carries a
    ``synthetic:`` source of its own.
    """
    db_session.add(detector_labelled_sensor_row(anomaly_type="UNDER_HEATING", index=0))
    await db_session.commit()

    result = await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
    )

    assert result["deleted_sensor"] == 0
    assert await count_sensor_rows(db_session) == 1

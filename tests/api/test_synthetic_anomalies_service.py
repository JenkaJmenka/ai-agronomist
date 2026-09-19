"""Behavioural tests for the synthetic anomaly inject/remove service.

Complements ``test_synthetic_anomalies_gas_isolation.py`` (which pins the
climate-``co2`` vs gas-panel-``gas_co2`` separation) by covering the rest of
the service contract: request validation, window resolution, the no-raw-data
fallback, the score bands from ADR-005 §2.2, and the removal semantics that
guarantee real sensor data survives.
"""

from __future__ import annotations

import datetime
import random

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.synthetic_anomalies import (
    ANOMALY_TYPES,
    delete_synthetic_anomalies,
    inject_synthetic_anomalies,
)
from src.config import SYNTHETIC_ANOMALY_SCORE_RANGE, SYNTHETIC_CONTEXT_SCORE_RANGE
from src.db.models import Base, RawReading, SensorReading

TENANT = "tenant_synthetic_service"
OTHER_TENANT = "tenant_synthetic_other"
GREENHOUSE = "gh_synthetic_service"

_RNG_SEED = 90210
_HISTORY_ROWS = 60
_HISTORY_START = datetime.datetime(2026, 4, 1, 0, 0, tzinfo=datetime.timezone.utc)
_HISTORY_STEP = datetime.timedelta(minutes=5)
_WINDOW_START = "2026-04-05T00:00:00+00:00"
_WINDOW_END = "2026-04-07T00:00:00+00:00"

_DEFAULT_WINDOW_HOURS = 48


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture(autouse=True)
def seed_service_rng() -> None:
    random.seed(_RNG_SEED)


async def _seed_raw_history(db: AsyncSession, *, extra_columns: dict | None = None) -> None:
    """Seed RawReading history with per-column Gaussian noise (never constants)."""
    rng = random.Random(_RNG_SEED)
    db.add_all(
        [
            RawReading(
                tenant_id=TENANT,
                greenhouse_id=GREENHOUSE,
                timestamp=_HISTORY_START + i * _HISTORY_STEP,
                source="csv",
                data={
                    "temperature": round(rng.gauss(23.0, 1.5), 2),
                    "humidity": round(rng.gauss(63.0, 4.0), 2),
                    "co2": round(rng.gauss(420.0, 30.0), 2),
                    "light": round(rng.gauss(400.0, 90.0), 2),
                    **(extra_columns or {}),
                },
            )
            for i in range(_HISTORY_ROWS)
        ]
    )
    await db.commit()


async def _inject(db: AsyncSession, **overrides) -> dict:
    params = {
        "tenant_id": TENANT,
        "greenhouse_id": GREENHOUSE,
        "n_normal": 10,
        "n_anomalies": 10,
        "anomaly_type": "co2_surge",
        "window_start": _WINDOW_START,
        "window_end": _WINDOW_END,
    }
    params.update(overrides)
    return await inject_synthetic_anomalies(db, **params)


def _synthetic_sensor_row(
    *, anomaly_type: str | None, index: int, tenant_id: str = TENANT
) -> SensorReading:
    return SensorReading(
        tenant_id=tenant_id,
        greenhouse_id=GREENHOUSE,
        timestamp=_HISTORY_START + index * _HISTORY_STEP,
        temperature=22.0,
        humidity=60.0,
        co2=430.0,
        light=300.0,
        anomaly_score=0.9,
        anomaly_type=anomaly_type,
    )


def _synthetic_raw_row(*, source: str, index: int, tenant_id: str = TENANT) -> RawReading:
    return RawReading(
        tenant_id=tenant_id,
        greenhouse_id=GREENHOUSE,
        timestamp=_HISTORY_START + index * _HISTORY_STEP,
        source=source,
        data={"co2": 430.0},
    )


async def _count_sensor_rows(db: AsyncSession, tenant_id: str = TENANT) -> int:
    rows = await db.execute(select(SensorReading).where(SensorReading.tenant_id == tenant_id))
    return len(list(rows.scalars().all()))


async def _raw_sources(db: AsyncSession, tenant_id: str = TENANT) -> list[str | None]:
    rows = await db.execute(select(RawReading).where(RawReading.tenant_id == tenant_id))
    return [row.source for row in rows.scalars().all()]


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_reject_unknown_anomaly_type_on_inject(db_session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _inject(db_session, anomaly_type="tornado")
    assert exc_info.value.status_code == 422
    assert "tornado" in exc_info.value.detail


@pytest.mark.asyncio
async def test_should_reject_unparseable_window_datetime(db_session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _inject(db_session, window_start="not-a-date", window_end=_WINDOW_END)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_should_reject_window_end_at_or_before_window_start(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _inject(db_session, window_start=_WINDOW_END, window_end=_WINDOW_START)
    assert exc_info.value.status_code == 422
    assert "window_end" in exc_info.value.detail


@pytest.mark.asyncio
async def test_should_treat_naive_window_bounds_as_utc(db_session: AsyncSession) -> None:
    summary = await _inject(
        db_session, window_start="2026-04-05T00:00:00", window_end="2026-04-07T00:00:00"
    )
    assert summary["window_start"].endswith("+00:00")
    assert summary["window_hours"] == _DEFAULT_WINDOW_HOURS


@pytest.mark.asyncio
async def test_should_default_to_the_last_48_hours_when_no_window_given(
    db_session: AsyncSession,
) -> None:
    summary = await _inject(db_session, window_start=None, window_end=None)

    assert summary["window_hours"] == _DEFAULT_WINDOW_HOURS
    window_end = datetime.datetime.fromisoformat(summary["window_end"])
    window_start = datetime.datetime.fromisoformat(summary["window_start"])
    assert window_end - window_start == datetime.timedelta(hours=_DEFAULT_WINDOW_HOURS)
    assert abs((datetime.datetime.now(datetime.timezone.utc) - window_end).total_seconds()) < 60


# ---------------------------------------------------------------------------
# Column learning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_fall_back_to_canonical_columns_when_no_raw_history_exists(
    db_session: AsyncSession,
) -> None:
    summary = await _inject(db_session)

    assert summary["injected"] == 20
    assert summary["raw_injected"] == 0, "no learned columns means no raw rows are fabricated"
    assert set(summary["columns_used"]) == {"temperature", "humidity", "co2", "light"}


@pytest.mark.asyncio
async def test_should_ignore_non_numeric_raw_columns_when_learning_ranges(
    db_session: AsyncSession,
) -> None:
    await _seed_raw_history(db_session, extra_columns={"device_status": "ok"})

    summary = await _inject(db_session)

    assert "device_status" not in summary["columns_used"]
    assert "co2" in summary["columns_used"]


@pytest.mark.asyncio
async def test_should_mirror_learned_columns_into_injected_raw_rows(
    db_session: AsyncSession,
) -> None:
    await _seed_raw_history(db_session)

    summary = await _inject(db_session)

    assert summary["raw_injected"] == 20
    rows = await db_session.execute(
        select(RawReading).where(RawReading.source.like("synthetic:%"))
    )
    injected = list(rows.scalars().all())
    assert injected
    assert all(set(row.data) == set(summary["columns_used"]) for row in injected)


# ---------------------------------------------------------------------------
# Score bands (ADR-005 §2.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("anomaly_type", ANOMALY_TYPES)
async def test_should_score_anomaly_rows_inside_the_canonical_synthetic_band(
    db_session: AsyncSession, anomaly_type: str
) -> None:
    band_lo, band_hi = SYNTHETIC_ANOMALY_SCORE_RANGE
    await _inject(db_session, anomaly_type=anomaly_type)

    rows = await db_session.execute(
        select(SensorReading).where(SensorReading.anomaly_type == anomaly_type)
    )
    spike_rows = list(rows.scalars().all())
    assert len(spike_rows) == 10
    assert all(band_lo <= row.anomaly_score <= band_hi for row in spike_rows)
    assert all(row.score_scale == "synthetic" for row in spike_rows)
    assert all(row.anomaly_score_raw is None for row in spike_rows)


@pytest.mark.asyncio
async def test_should_score_context_rows_below_the_live_noise_floor(
    db_session: AsyncSession,
) -> None:
    context_lo, context_hi = SYNTHETIC_CONTEXT_SCORE_RANGE
    await _inject(db_session)

    rows = await db_session.execute(
        select(SensorReading).where(SensorReading.anomaly_type == "synthetic_context")
    )
    context_rows = list(rows.scalars().all())
    assert len(context_rows) == 10
    assert all(context_lo <= row.anomaly_score <= context_hi for row in context_rows)


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


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
            _synthetic_sensor_row(anomaly_type="co2_surge", index=0),
            _synthetic_sensor_row(anomaly_type="humidity_spike", index=1),
            _synthetic_raw_row(source="synthetic:co2_surge", index=0),
            _synthetic_raw_row(source="synthetic:humidity_spike", index=1),
        ]
    )
    await db_session.commit()

    result = await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type="co2_surge"
    )

    assert result["deleted_sensor"] == 1
    assert result["deleted_raw"] == 1
    assert await _raw_sources(db_session) == ["synthetic:humidity_spike"]


@pytest.mark.asyncio
@pytest.mark.parametrize("all_types_argument", [None, ""], ids=["omitted", "empty"])
async def test_should_treat_blank_anomaly_type_as_remove_all_synthetic(
    db_session: AsyncSession, all_types_argument: str | None
) -> None:
    db_session.add_all(
        [
            _synthetic_sensor_row(anomaly_type="co2_surge", index=0),
            _synthetic_sensor_row(anomaly_type="synthetic_context", index=1),
            _synthetic_raw_row(source="synthetic:co2_surge", index=0),
            _synthetic_raw_row(source="synthetic:context", index=1),
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
@pytest.mark.xfail(
    reason=(
        "BUG: whitespace-only anomaly_type 422s instead of meaning 'all'. "
        "delete_synthetic_anomalies strips the value but then rejects the "
        "resulting empty string, so '   ' raises "
        "\"Unknown anomaly_type ''\" — the strip() exists precisely to "
        "normalise that input (see the 'Treat empty string the same as omit' "
        "comment on the line above it)."
    ),
    strict=True,
)
async def test_should_treat_whitespace_only_anomaly_type_as_remove_all_synthetic(
    db_session: AsyncSession,
) -> None:
    db_session.add(_synthetic_sensor_row(anomaly_type="co2_surge", index=0))
    await db_session.commit()

    result = await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type="   "
    )

    assert result["anomaly_type"] == "all"
    assert result["deleted_sensor"] == 1


@pytest.mark.asyncio
async def test_should_never_delete_real_sensor_data(db_session: AsyncSession) -> None:
    """Untagged canonical rows and non-synthetic raw sources must survive."""
    await _seed_raw_history(db_session)
    db_session.add(_synthetic_sensor_row(anomaly_type=None, index=99))
    db_session.add(_synthetic_sensor_row(anomaly_type="co2_surge", index=98))
    await db_session.commit()

    await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
    )

    assert await _count_sensor_rows(db_session) == 1, "the untagged real row must survive"
    assert await _raw_sources(db_session) == ["csv"] * _HISTORY_ROWS


@pytest.mark.asyncio
async def test_should_leave_other_tenants_synthetic_rows_untouched(
    db_session: AsyncSession,
) -> None:
    db_session.add_all(
        [
            _synthetic_sensor_row(anomaly_type="co2_surge", index=0),
            _synthetic_sensor_row(anomaly_type="co2_surge", index=0, tenant_id=OTHER_TENANT),
            _synthetic_raw_row(source="synthetic:co2_surge", index=0),
            _synthetic_raw_row(source="synthetic:co2_surge", index=0, tenant_id=OTHER_TENANT),
        ]
    )
    await db_session.commit()

    await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
    )

    assert await _count_sensor_rows(db_session, OTHER_TENANT) == 1
    assert await _raw_sources(db_session, OTHER_TENANT) == ["synthetic:co2_surge"]


@pytest.mark.asyncio
async def test_should_delete_every_row_carrying_any_anomaly_type_label(
    db_session: AsyncSession,
) -> None:
    """Pins current behaviour: "remove all synthetic" keys off *any* non-null
    ``anomaly_type``, not off a synthetic marker.

    ``src/api/services/fault_classification.py:73-76`` documents two further
    writers of that column (setpoint_scada_detector, band_breach_detector, both
    UPPER_CASE labels).  Should either ever persist to ``SensorReading``, this
    assertion flips from "pinned behaviour" to "detector-labelled real rows are
    silently destroyed" — see the QA report.
    """
    db_session.add(_synthetic_sensor_row(anomaly_type="UNDER_HEATING", index=0))
    await db_session.commit()

    result = await delete_synthetic_anomalies(
        db_session, tenant_id=TENANT, greenhouse_id=GREENHOUSE, anomaly_type=None
    )

    assert result["deleted_sensor"] == 1
    assert await _count_sensor_rows(db_session) == 0

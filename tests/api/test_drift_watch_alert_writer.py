"""ADR-013 steps 4-6: alert gate persistence, alert writer, feedback, enable.

The end-to-end cases run the real service on 36 weeks of seeded hourly data in
which the plant probe reads 3 C low for the last 10 days (a +3 C paired-probe
episode, out-of-sample, recent, above the 2 x min_shift limit). SQLite, ~10 s.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.config as config
from src.api.services.drift_watch_alerts import (
    ALERT_DETECTOR,
    AlertEnableError,
    build_drift_alert_message,
    set_alert_state_manually,
)
from src.api.services.drift_watch_service import (
    backfill_drift_watch,
    run_drift_watch_shadow,
)
from src.db.models import (
    AnomalyEpisodeRecord,
    Base,
    DriftWatchEpisode,
    DriftWatchFeedback,
    DriftWatchInput,
    RawReading,
)
from src.models.drift_watch import DriftInputSpec, InputKind

T = "t_alert"
G = "g_alert"
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
WEEKS = 36
N = WEEKS * 168
DATA_END = T0 + dt.timedelta(hours=N - 1)


def _rows() -> list[RawReading]:
    rnd = random.Random(1)
    rows = []
    for i in range(N):
        ph = 2 * math.pi * (i % 24) / 24
        out = 10 + 5 * math.sin(2 * math.pi * i / (24 * 365)) + 4 * math.sin(ph) + rnd.gauss(0, 0.5)
        t = 22 + 1.5 * math.sin(ph) + 0.1 * out + rnd.gauss(0, 0.15)
        pt = t - 1 + rnd.gauss(0, 0.15)
        if i > N - 24 * 10:
            pt -= 3.0
        rows.append(
            RawReading(
                tenant_id=T, greenhouse_id=G, timestamp=T0 + dt.timedelta(hours=i),
                source="csv",
                data={
                    "temperature": t,
                    "humidity": 65 + 3 * math.sin(ph) + rnd.gauss(0, 1),
                    "co2": 400 + 15 * math.sin(ph) + rnd.gauss(0, 5),
                    "light": max(0, 60 * math.sin(math.pi * ((i % 24) - 6) / 12)),
                    "measured_plant_temp": pt,
                    "outdoor_temperature": out,
                },
            )
        )
    return rows


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add_all(_rows())
        await session.commit()
        yield session
    await engine.dispose()


async def _alerts(db: AsyncSession) -> list[AnomalyEpisodeRecord]:
    return list(
        (
            await db.execute(
                select(AnomalyEpisodeRecord).where(
                    AnomalyEpisodeRecord.detector == ALERT_DETECTOR
                )
            )
        ).scalars()
    )


async def _input(db: AsyncSession, input_id: str = "temp_paired") -> DriftWatchInput:
    row = (
        await db.execute(
            select(DriftWatchInput).where(
                DriftWatchInput.tenant_id == T,
                DriftWatchInput.greenhouse_id == G,
                DriftWatchInput.input_id == input_id,
            )
        )
    ).scalar_one()
    await db.refresh(row)
    return row


async def _upload(db: AsyncSession) -> None:
    await run_drift_watch_shadow(
        db, T, G, DATA_END - dt.timedelta(days=7), DATA_END + dt.timedelta(hours=1)
    )


@pytest.mark.asyncio
async def test_flag_off_writes_no_alert_and_state_stays_off(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", False)
    await backfill_drift_watch(db, T, G)
    # Even a forced 'enabled' row must not alert, and the gate forces it off.
    row = await _input(db)
    row.alert_state = "enabled"
    await db.commit()
    await _upload(db)
    await _upload(db)
    assert await _alerts(db) == []
    row = await _input(db)
    assert row.alert_state == "off"
    assert "disabled globally" in (row.alert_state_reason or "")


@pytest.mark.asyncio
async def test_one_alert_across_uploads_and_backfill_and_feedback_survives(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", True)

    await backfill_drift_watch(db, T, G)
    assert await _alerts(db) == []  # backfill writes no alerts
    row = await _input(db)
    assert row.alert_state == "eligible", row.alert_state_reason
    eps = (
        await db.execute(
            select(DriftWatchEpisode).where(DriftWatchEpisode.input_id == "temp_paired")
        )
    ).scalars().all()
    big = [e for e in eps if e.offset_robust is not None and abs(e.offset_robust) >= 1.0]
    assert len(big) == 1 and big[0].direction == "+"

    await set_alert_state_manually(db, row, enable=True)
    await db.commit()

    await _upload(db)
    alerts = await _alerts(db)
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.severity, a.fault_type, a.channel) == ("warning", "calibration_drift", "temperature")
    assert a.trigger_value == pytest.approx(big[0].offset_robust)
    assert a.release_ts is None

    await _upload(db)
    assert len(await _alerts(db)) == 1

    # Label, then backfill: the label survives, the state is recomputed from the
    # prior 'enabled', and the backfill adds no second alert.
    db.add(
        DriftWatchFeedback(
            tenant_id=T, greenhouse_id=G, input_id="temp_paired", direction="+",
            first_detected_ts=big[0].first_detected_ts, label="confirmed_drift",
        )
    )
    await db.commit()
    await backfill_drift_watch(db, T, G)
    assert (await db.execute(select(func.count()).select_from(DriftWatchFeedback))).scalar_one() == 1
    assert len(await _alerts(db)) == 1
    assert (await _input(db)).alert_state == "enabled"
    await _upload(db)
    assert len(await _alerts(db)) == 1


@pytest.mark.asyncio
async def test_stuck_fault_on_a_read_channel_suppresses_the_alert(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", True)
    await backfill_drift_watch(db, T, G)
    row = await _input(db)
    await set_alert_state_manually(db, row, enable=True)
    # A real stuck_fault on the REFERENCE probe, not the target channel.
    start = DATA_END - dt.timedelta(days=8)
    db.add(
        AnomalyEpisodeRecord(
            id="stuck-1", tenant_id=T, greenhouse_id=G,
            channel="measured_plant_temp", detector="stuck_fault",
            fault_type="stuck_fault", onset_ts=start, end_ts=DATA_END,
            detected_ts=start, severity="critical", is_ongoing=False,
            frozen_sample_count=10, raw_slot_count=10,
        )
    )
    await db.commit()
    await _upload(db)
    assert await _alerts(db) == []
    ep = (
        await db.execute(
            select(DriftWatchEpisode).where(
                DriftWatchEpisode.input_id == "temp_paired",
                DriftWatchEpisode.offset_robust > 1.0,
            )
        )
    ).scalars().one()
    assert ep.suppressed_by == "stuck_fault"


@pytest.mark.asyncio
async def test_enable_rejected_unless_eligible_and_flag_on(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", True)
    await backfill_drift_watch(db, T, G)
    rh = await _input(db, "rh_virtual")
    for not_eligible in ("off", "suspended", "enabled"):
        rh.alert_state = not_eligible
        with pytest.raises(AlertEnableError):
            await set_alert_state_manually(db, rh, enable=True)
        assert rh.alert_state == not_eligible

    tp = await _input(db)
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", False)
    with pytest.raises(AlertEnableError):
        await set_alert_state_manually(db, tp, enable=True)

    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", True)
    await set_alert_state_manually(db, tp, enable=True)
    assert tp.alert_state == "enabled"
    await set_alert_state_manually(db, tp, enable=False)
    assert tp.alert_state == "off"


@pytest.mark.asyncio
async def test_old_calibration_without_window_end_stays_off(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", True)
    await backfill_drift_watch(db, T, G)
    row = await _input(db)
    cal = json.loads(row.calibration_json)
    cal.pop("calibration_window_end")
    row.calibration_json = json.dumps(cal)
    row.alert_state = "off"
    await db.commit()
    await _upload(db)
    assert (await _input(db)).alert_state == "off"


def test_paired_probe_message_never_names_one_probe() -> None:
    spec = DriftInputSpec(
        input_id="temp_paired", kind=InputKind.PAIRED_PROBES,
        target_channel="temperature", channels=["measured_plant_temp"], min_shift=0.5,
    )
    for direction, off in (("+", 2.94), ("-", -2.94)):
        msg = build_drift_alert_message(spec, direction, off)
        assert msg == (
            "Greenhouse air temperature and plant temperature disagree by 2.9 °C. "
            "One of the two probes may have drifted."
        )
    rh = DriftInputSpec(
        input_id="rh_virtual", kind=InputKind.VIRTUAL_SENSOR, target_channel="humidity",
        channels=["temperature", "co2", "light"], min_shift=2.0,
    )
    assert build_drift_alert_message(rh, "-", -6.0) == (
        "RH reads 6.0 %RH below what temperature, CO₂ and light predict."
    )

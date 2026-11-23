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


# ── ADR-013 step 8 gaps: unit-level, empty in-memory DB (no seeded scope) ──
#
# The seeded tests above exercise the real pipeline once per behaviour. The
# cases below pin the writer rules those runs cannot isolate: recency, the
# robust (not single-hour) offset limit, one alert across a refit/backfill
# continuation, and stuck suppression on the target and on a predictor.

from src.api.services.drift_watch_alerts import refresh_alert_state, write_drift_alerts

_AS_OF = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
_TP_SPEC = DriftInputSpec(
    input_id="temp_paired", kind=InputKind.PAIRED_PROBES, target_channel="temperature",
    channels=["measured_plant_temp", "outdoor_temperature"], min_shift=0.5,
)  # alert limit = 2 x 0.5 = 1.0 C
_RH_SPEC = DriftInputSpec(
    input_id="rh_virtual", kind=InputKind.VIRTUAL_SENSOR, target_channel="humidity",
    channels=["temperature", "co2", "light"], min_shift=2.0,
)  # alert limit = 4.0 %RH


@pytest_asyncio.fixture
async def empty_db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _ep(
    spec: DriftInputSpec, *, first: dt.datetime, last: dt.datetime,
    robust: float | None, single_hour: float | None = None,
    onset: dt.datetime | None = None, episode_id: str = "e_0", direction: str = "+",
) -> DriftWatchEpisode:
    return DriftWatchEpisode(
        tenant_id=T, greenhouse_id=G, input_id=spec.input_id, episode_id=episode_id,
        target_channel=spec.target_channel, direction=direction,
        first_detected_ts=first, last_confirmed_ts=last,
        onset_estimate_ts=onset or first - dt.timedelta(hours=6),
        offset_estimate=single_hour, offset_robust=robust,
        input_status_at_detection="armed",
    )


_FAR_OOS = dt.timedelta(days=30)  # calibration window ended well before the episode


@pytest.mark.asyncio
@pytest.mark.parametrize("robust,single_hour,age,written,first_minus_cal_end", [
    # Offset limit uses offset_robust, never offset_estimate (current_offset_native).
    pytest.param(1.2, 0.3, dt.timedelta(days=1), True, _FAR_OOS, id="robust-over-limit-single-hour-under"),
    pytest.param(0.8, 7.6, dt.timedelta(days=1), False, _FAR_OOS, id="robust-under-limit-single-hour-over"),
    pytest.param(None, 7.6, dt.timedelta(days=1), False, _FAR_OOS, id="robust-unknown"),
    # Recency: last_confirmed within ALERT_RECENCY_DAYS (14) of the newest hour.
    pytest.param(2.0, 2.0, dt.timedelta(days=14), True, _FAR_OOS, id="recency-edge-inclusive"),
    pytest.param(2.0, 2.0, dt.timedelta(days=14, hours=1), False, _FAR_OOS, id="recency-just-outside"),
    # In-sample (ADR-013 §4): first_detected_ts at or before calibration_window_end.
    pytest.param(2.0, 2.0, dt.timedelta(days=1), False, dt.timedelta(0), id="in-sample-at-window-end"),
    pytest.param(2.0, 2.0, dt.timedelta(days=1), True, dt.timedelta(hours=1), id="out-of-sample-hour-after"),
])
async def test_writer_robust_offset_limit_and_recency(
    empty_db: AsyncSession, robust, single_hour, age, written, first_minus_cal_end
) -> None:
    assert config.DRIFT_WATCH_ALERT_RECENCY_DAYS == 14
    last = _AS_OF - age
    first = last - dt.timedelta(days=2)
    ep = _ep(_TP_SPEC, first=first, last=last, robust=robust, single_hour=single_hour)
    n = await write_drift_alerts(
        empty_db, T, G, _TP_SPEC, [ep], as_of_ts=str(_AS_OF),
        calibration_window_end=str(first - first_minus_cal_end),
    )
    alerts = await _alerts(empty_db)
    assert (n, len(alerts)) == ((1, 1) if written else (0, 0))
    if written:
        assert alerts[0].trigger_value == pytest.approx(robust)


@pytest.mark.asyncio
async def test_writer_one_alert_across_refit_and_backfill_continuation(
    empty_db: AsyncSession,
) -> None:
    """A continuation keeps first_detected_ts; a refit or backfill may move the
    onset estimate and renumber episode_id. Still one alert, updated in place.
    Only a new episode (after DRIFT_WATCH_QUIET_HOURS) adds a second alert."""
    first = _AS_OF - dt.timedelta(days=6)
    onset0 = first - dt.timedelta(hours=10)
    e0 = _ep(_TP_SPEC, first=first, last=first + dt.timedelta(days=1), robust=1.5, onset=onset0)
    await write_drift_alerts(empty_db, T, G, _TP_SPEC, [e0], as_of_ts=str(_AS_OF),
        calibration_window_end=str(_AS_OF - dt.timedelta(weeks=20)))

    later_last = first + dt.timedelta(days=3)
    e1 = _ep(_TP_SPEC, first=first, last=later_last, robust=2.0,
             onset=first - dt.timedelta(hours=30), episode_id="temp_paired_7")
    await write_drift_alerts(empty_db, T, G, _TP_SPEC, [e1], as_of_ts=str(_AS_OF),
        calibration_window_end=str(_AS_OF - dt.timedelta(weeks=20)))
    alerts = await _alerts(empty_db)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.trigger_value == pytest.approx(2.0)
    assert a.end_ts.replace(tzinfo=dt.timezone.utc) == later_last
    assert a.onset_ts.replace(tzinfo=dt.timezone.utc) == onset0  # not moved by the refit

    new_first = later_last + dt.timedelta(hours=config.DRIFT_WATCH_QUIET_HOURS + 1)
    e2 = _ep(_TP_SPEC, first=new_first, last=new_first, robust=1.5, episode_id="temp_paired_8")
    await write_drift_alerts(empty_db, T, G, _TP_SPEC, [e1, e2], as_of_ts=str(_AS_OF),
        calibration_window_end=str(_AS_OF - dt.timedelta(weeks=20)))
    assert len(await _alerts(empty_db)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stuck_channel,suppressed", [
    pytest.param("humidity", True, id="target"),
    pytest.param("temperature", True, id="predictor"),
    pytest.param("measured_plant_temp", False, id="channel-not-read-by-input"),
])
async def test_stuck_fault_suppression_on_target_and_predictor(
    empty_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, stuck_channel: str, suppressed: bool
) -> None:
    monkeypatch.setattr(config, "DRIFT_WATCH_ALERTS_ENABLED", True)
    cal_end = _AS_OF - dt.timedelta(weeks=20)
    row = DriftWatchInput(
        tenant_id=T, greenhouse_id=G, input_id=_RH_SPEC.input_id, spec_json="{}",
        calibration_json=json.dumps({"calibration_window_end": str(cal_end)}),
        status="armed", alert_state="enabled",
    )
    last = _AS_OF - dt.timedelta(hours=2)
    first = last - dt.timedelta(days=2)
    empty_db.add_all([
        row,
        _ep(_RH_SPEC, first=first, last=last, robust=-6.0, single_hour=-6.0, direction="-"),
        AnomalyEpisodeRecord(
            id="stuck-u", tenant_id=T, greenhouse_id=G, channel=stuck_channel,
            detector="stuck_fault", fault_type="stuck_fault",
            onset_ts=first - dt.timedelta(days=1), end_ts=first + dt.timedelta(hours=3),
            detected_ts=first, severity="critical", is_ongoing=False,
            frozen_sample_count=10, raw_slot_count=10,
        ),
    ])
    await empty_db.commit()

    await refresh_alert_state(empty_db, T, G, _RH_SPEC, row, as_of_ts=str(_AS_OF))
    ep = (await empty_db.execute(select(DriftWatchEpisode))).scalar_one()
    assert row.alert_state == "enabled", row.alert_state_reason
    assert ep.suppressed_by == ("stuck_fault" if suppressed else None)
    assert len(await _alerts(empty_db)) == (0 if suppressed else 1)

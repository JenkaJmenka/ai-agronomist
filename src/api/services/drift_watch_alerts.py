"""ADR-013 drift-watch alert policy: gate evaluation and the alert writer.

Called from ``drift_watch_service`` after an input has been scored. The pure
policy (counts, transitions, the offset limit) lives in
``src.models.drift_watch``; this module loads the stored episodes, labels and
stuck-fault hits, applies it, persists ``alert_state`` and, only for an
``enabled`` input with the global flag on, writes one ``anomaly_episodes`` row
per drift episode.

Keying. A drift alert is found by ``(tenant, greenhouse, channel,
detector='drift_watch', detected_ts = episode.first_detected_ts)``. That is the
ADR §4 natural key (the input and direction are implied by the channel and the
time), and it does not depend on ``onset_ts``, which is an estimate that can
move between a backfill and an upload. A refit, a re-upload or a backfill
therefore never creates a second alert.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import src.config as config
from src.db.models import (
    AnomalyEpisodeRecord,
    DriftWatchEpisode,
    DriftWatchFeedback,
    DriftWatchInput,
)
from src.models.drift_watch import (
    AlertState,
    DriftEpisodeKey,
    DriftInputSpec,
    GateEpisode,
    InputKind,
    episode_natural_key,
    evaluate_alert_gate,
    passes_alert_min_offset,
)

logger = logging.getLogger(__name__)

ALERT_DETECTOR = "drift_watch"
ALERT_FAULT_TYPE = "calibration_drift"
ALERT_SEVERITY = "warning"  # ADR-013 §4: advisory, never urgent/critical.
SUPPRESSED_BY_STUCK = "stuck_fault"
_STUCK_DETECTOR = "stuck_fault"


def _utc(dt: datetime) -> datetime:
    """Return *dt* as aware UTC (SQLite rows come back naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_PRETTY_CHANNEL = {"temperature": "temperature", "co2": "CO₂", "light": "light"}


def _pretty(channel: str) -> str:
    return _PRETTY_CHANNEL.get(channel, channel.replace("_", " "))


def build_drift_alert_message(
    spec: DriftInputSpec,
    direction: str,
    offset_robust: float,
) -> str:
    """Operator wording for a drift alert (ADR-013 §4).

    For paired probes the residual cannot say which probe drifted, so the text
    never names one probe as the faulty one.
    """
    size = f"{abs(offset_robust):.1f}"
    above = direction == "+"
    if spec.kind == InputKind.PAIRED_PROBES:
        return (
            f"Greenhouse air temperature and plant temperature disagree by "
            f"{size} °C. One of the two probes may have drifted."
        )
    if spec.kind == InputKind.VIRTUAL_SENSOR:
        names = [_pretty(c) for c in spec.channels]
        if len(names) > 1:
            predictors = ", ".join(names[:-1]) + " and " + names[-1]
        else:
            predictors = ", ".join(names) or "the other sensors"
        return (
            f"RH reads {size} %RH {'above' if above else 'below'} what "
            f"{predictors} predict."
        )
    return (
        f"{spec.target_channel.replace('_', ' ')} reads {size} g/m³ "
        f"{'above' if above else 'below'} the value computed from temperature "
        f"and RH."
    )


async def load_labels(
    db: AsyncSession, tenant_id: str, greenhouse_id: str, input_id: str,
) -> dict[DriftEpisodeKey, str]:
    """Operator labels for one input, keyed on the natural key."""
    rows = (
        await db.execute(
            select(DriftWatchFeedback).where(
                DriftWatchFeedback.tenant_id == tenant_id,
                DriftWatchFeedback.greenhouse_id == greenhouse_id,
                DriftWatchFeedback.input_id == input_id,
            )
        )
    ).scalars().all()
    out: dict[DriftEpisodeKey, str] = {}
    for r in rows:
        out[
            episode_natural_key(
                r.tenant_id, r.greenhouse_id, r.input_id, r.direction,
                r.first_detected_ts,
            )
        ] = r.label
    return out


async def _stuck_spans(
    db: AsyncSession, tenant_id: str, greenhouse_id: str, channels: Sequence[str],
) -> list[tuple[str, datetime, datetime]]:
    """Real (non-synthetic) stuck_fault spans on any of *channels*."""
    rows = (
        await db.execute(
            select(AnomalyEpisodeRecord).where(
                AnomalyEpisodeRecord.tenant_id == tenant_id,
                AnomalyEpisodeRecord.greenhouse_id == greenhouse_id,
                AnomalyEpisodeRecord.detector == _STUCK_DETECTOR,
                AnomalyEpisodeRecord.channel.in_(list(channels)),
            )
        )
    ).scalars().all()
    spans: list[tuple[str, datetime, datetime]] = []
    for r in rows:
        if r.source is not None and r.source.startswith(config.SYNTHETIC_SOURCE_PREFIX):
            continue
        # A freeze keeps the residual offset until release, so the span runs
        # to release_ts when known, else the last frozen sample.
        end = r.release_ts or r.end_ts
        spans.append((r.channel, _utc(r.onset_ts), _utc(end)))
    return spans


def _suppressed(
    ep: DriftWatchEpisode, spans: Sequence[tuple[str, datetime, datetime]],
) -> Optional[str]:
    start = _utc(ep.onset_estimate_ts or ep.first_detected_ts)
    end = _utc(ep.last_confirmed_ts)
    for _channel, s, e in spans:
        if s <= end and e >= start:
            return SUPPRESSED_BY_STUCK
    return None


def _spec_channels(spec: DriftInputSpec) -> list[str]:
    return sorted({spec.target_channel, *spec.channels})


async def refresh_alert_state(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    spec: DriftInputSpec,
    input_row: DriftWatchInput,
    *,
    as_of_ts: Optional[str],
    write_alerts: bool = True,
) -> None:
    """Re-evaluate the ADR-013 gate for one input and write alerts if enabled.

    Never raises: a failure is logged and leaves the alert state unchanged, so
    a policy problem cannot break the upload path.
    """
    try:
        await _refresh(
            db, tenant_id, greenhouse_id, spec, input_row,
            as_of_ts=as_of_ts, write_alerts=write_alerts,
        )
    except Exception:
        logger.warning(
            "drift_watch alert refresh failed for %s/%s/%s; alert_state unchanged",
            tenant_id, greenhouse_id, spec.input_id, exc_info=True,
        )


async def _refresh(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    spec: DriftInputSpec,
    input_row: DriftWatchInput,
    *,
    as_of_ts: Optional[str],
    write_alerts: bool,
) -> None:
    import json

    if input_row.calibration_json is None or as_of_ts is None:
        return
    # The immutable copy. NOT state_json's embedded calibration, which every
    # rolling refit rewrites (see CalibrationResult.from_dict).
    cal: dict[str, Any] = json.loads(input_row.calibration_json)
    cal_end = cal.get("calibration_window_end")

    ep_rows = list(
        (
            await db.execute(
                select(DriftWatchEpisode).where(
                    DriftWatchEpisode.tenant_id == tenant_id,
                    DriftWatchEpisode.greenhouse_id == greenhouse_id,
                    DriftWatchEpisode.input_id == spec.input_id,
                )
            )
        ).scalars().all()
    )
    spans = await _stuck_spans(db, tenant_id, greenhouse_id, _spec_channels(spec))
    for row in ep_rows:
        sup = _suppressed(row, spans)
        if row.suppressed_by != sup:
            row.suppressed_by = sup
    labels = await load_labels(db, tenant_id, greenhouse_id, spec.input_id)

    flag = bool(config.DRIFT_WATCH_ALERTS_ENABLED)
    try:
        gate_eps = [
            GateEpisode(
                key=episode_natural_key(
                    tenant_id, greenhouse_id, spec.input_id,
                    r.direction, r.first_detected_ts,
                ),
                offset_robust=r.offset_robust,
                suppressed=r.suppressed_by is not None,
            )
            for r in ep_rows
        ]
        decision = evaluate_alert_gate(
            gate_eps, labels,
            current_state=AlertState(input_row.alert_state),
            input_status=input_row.status,
            calibration_window_end=cal_end,
            as_of=as_of_ts,
            min_shift=spec.min_shift,
            alerts_enabled=flag,
        )
    except ValueError as exc:
        logger.warning(
            "drift_watch gate not evaluated for %s/%s/%s: %s",
            tenant_id, greenhouse_id, spec.input_id, exc,
        )
        return

    if decision.next_state.value != input_row.alert_state:
        input_row.alert_state = decision.next_state.value
        input_row.alert_state_reason = decision.reason
        input_row.alert_state_changed_at = datetime.now(timezone.utc)
    await db.flush()

    if write_alerts and flag and input_row.alert_state == AlertState.ENABLED.value:
        async with db.begin_nested():
            await write_drift_alerts(
                db, tenant_id, greenhouse_id, spec, ep_rows, as_of_ts=as_of_ts,
                calibration_window_end=cal_end,
            )


async def write_drift_alerts(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    spec: DriftInputSpec,
    ep_rows: Sequence[DriftWatchEpisode],
    *,
    as_of_ts: str,
    calibration_window_end: Optional[str],
) -> int:
    """Upsert one ``anomaly_episodes`` row per qualifying episode.

    Qualifying: not suppressed, robust offset known and at least
    ``2 x min_shift``, last confirmed within ``DRIFT_WATCH_ALERT_RECENCY_DAYS``
    of the newest scored hour, and out-of-sample: ``first_detected_ts`` after
    *calibration_window_end* (the gate does not count in-sample episodes, so
    neither does the writer). An unknown *calibration_window_end* writes
    nothing. Returns the number of rows inserted or updated.
    """
    if not calibration_window_end:
        return 0
    cal_end = _utc(pd.Timestamp(calibration_window_end).to_pydatetime())
    as_of = _utc(pd.Timestamp(as_of_ts).to_pydatetime())
    recency_floor = as_of - timedelta(days=config.DRIFT_WATCH_ALERT_RECENCY_DAYS)
    quiet = timedelta(hours=config.DRIFT_WATCH_QUIET_HOURS)
    touched = 0
    for ep in ep_rows:
        if ep.suppressed_by is not None or ep.offset_robust is None:
            continue
        if not passes_alert_min_offset(ep.offset_robust, spec.min_shift):
            continue
        last = _utc(ep.last_confirmed_ts)
        if last < recency_floor:
            continue
        first = _utc(ep.first_detected_ts)
        if first <= cal_end:
            continue
        onset = _utc(ep.onset_estimate_ts) if ep.onset_estimate_ts else first
        onset = min(onset, last)
        ongoing = last >= as_of - quiet
        duration = int((last - onset).total_seconds())

        existing = (
            await db.execute(
                select(AnomalyEpisodeRecord).where(
                    AnomalyEpisodeRecord.tenant_id == tenant_id,
                    AnomalyEpisodeRecord.greenhouse_id == greenhouse_id,
                    AnomalyEpisodeRecord.channel == ep.target_channel,
                    AnomalyEpisodeRecord.detector == ALERT_DETECTOR,
                    AnomalyEpisodeRecord.detected_ts == first,
                )
            )
        ).scalars().first()
        if existing is None:
            db.add(
                AnomalyEpisodeRecord(
                    id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    greenhouse_id=greenhouse_id,
                    channel=ep.target_channel,
                    detector=ALERT_DETECTOR,
                    fault_type=ALERT_FAULT_TYPE,
                    onset_ts=onset,
                    end_ts=last,
                    detected_ts=first,
                    release_ts=None,
                    duration_seconds=duration,
                    severity=ALERT_SEVERITY,
                    trigger_value=float(ep.offset_robust),
                    is_ongoing=ongoing,
                    source=None,
                )
            )
        else:
            existing.end_ts = last
            existing.duration_seconds = int(
                (last - _utc(existing.onset_ts)).total_seconds()
            )
            existing.trigger_value = float(ep.offset_robust)
            existing.is_ongoing = ongoing
            existing.severity = ALERT_SEVERITY
        touched += 1
    await db.flush()
    return touched


class AlertEnableError(Exception):
    """Raised when a manual enable is refused (maps to HTTP 409)."""


async def set_alert_state_manually(
    db: AsyncSession,
    input_row: DriftWatchInput,
    *,
    enable: bool,
    reason: Optional[str] = None,
) -> DriftWatchInput:
    """Manual transition (ADR-013 §3): ``eligible -> enabled`` or any -> ``off``.

    Enabling is refused unless the global flag is on AND the input is
    ``eligible``. Disabling is always allowed. The caller owns authentication
    and the commit.
    """
    if enable:
        if not config.DRIFT_WATCH_ALERTS_ENABLED:
            raise AlertEnableError("drift-watch alerts are disabled globally")
        if input_row.alert_state != AlertState.ELIGIBLE.value:
            raise AlertEnableError(
                f"input is {input_row.alert_state!r}, not 'eligible'"
            )
        target = AlertState.ENABLED
        default_reason = "enabled by operator"
    else:
        target = AlertState.OFF
        default_reason = "disabled by operator"
    input_row.alert_state = target.value
    input_row.alert_state_reason = reason or default_reason
    input_row.alert_state_changed_at = datetime.now(timezone.utc)
    await db.flush()
    return input_row


async def oos_counts_for_input(
    db: AsyncSession, input_row: DriftWatchInput,
) -> tuple[Optional[int], Optional[int]]:
    """``(count, raw_count)`` for the inputs listing, or ``(None, None)``.

    ``None`` when the input is not scored yet or the count cannot be computed.
    """
    import json

    from src.models.drift_watch import DriftInputState, count_oos_false_alarms

    if input_row.calibration_json is None or input_row.state_json is None:
        return None, None
    try:
        cal = json.loads(input_row.calibration_json)
        spec = json.loads(input_row.spec_json)
        state = DriftInputState.from_dict(json.loads(input_row.state_json))
        if state.last_processed_ts is None:
            return None, None
        eps = (
            await db.execute(
                select(DriftWatchEpisode).where(
                    DriftWatchEpisode.tenant_id == input_row.tenant_id,
                    DriftWatchEpisode.greenhouse_id == input_row.greenhouse_id,
                    DriftWatchEpisode.input_id == input_row.input_id,
                )
            )
        ).scalars().all()
        labels: Mapping[DriftEpisodeKey, str] = await load_labels(
            db, input_row.tenant_id, input_row.greenhouse_id, input_row.input_id,
        )
        gate_eps = [
            GateEpisode(
                key=episode_natural_key(
                    r.tenant_id, r.greenhouse_id, r.input_id, r.direction,
                    r.first_detected_ts,
                ),
                offset_robust=r.offset_robust,
                suppressed=r.suppressed_by is not None,
            )
            for r in eps
        ]
        res = count_oos_false_alarms(
            gate_eps, labels,
            calibration_window_end=cal.get("calibration_window_end"),
            as_of=state.last_processed_ts,
            min_shift=float(spec["min_shift"]),
        )
        return res.count, res.raw_count
    except (ValueError, KeyError, TypeError):
        logger.warning(
            "oos count unavailable for %s/%s/%s",
            input_row.tenant_id, input_row.greenhouse_id, input_row.input_id,
            exc_info=True,
        )
        return None, None

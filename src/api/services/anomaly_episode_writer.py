"""Persists detection-time stuck-fault episodes to the ``anomaly_episodes`` table.

``finalize_scores`` (``src/api/services/upload_portions.py``) is session-less
by design — it builds the ``UploadInferenceResponse`` and does not touch the
database.  This module is the writer that both upload call sites (sync
``cold_run_upload.run_cold_run_upload`` and async
``upload_consumer.handle_upload_finalize``) invoke right after
``finalize_scores`` returns, when a DB session is actually available.

Spec: ``docs/handoff/task3_4_spec.md`` (schema) is the source of truth for
every column; this module is Part B, section 8, items 2-3 of that document.

Two sources, never conflated (task3_4_spec.md section "Read episodes from
exactly two sources"):

- **Released** — ``stuck_carry.completed_faults`` where ``fw.has_fired``.
  ``stuck_carry.recovery_channels`` is NEVER used here: it is a per-channel
  dict that overwrites on every release, so it silently drops all but the
  most recent released window per channel.
- **Open** — ``stuck_carry.channels`` where
  ``stuck_carry.has_onset_fired(channel, state.onset_ts)``.  A freeze that
  fired the gate but never released has no ``FaultWindow`` at all, so this
  is the only place that episode exists.

``detected_ts``, ``severity`` and ``trigger_value`` are not tracked on
``FaultWindow``/``ChannelFreezeState`` — they travel on the matching
stuck_fault alert in ``UploadInferenceResponse.high_anomalies`` instead
(keyed by ``(column, timestamp)`` == ``(channel, onset_ts)``), so this writer
takes that list as well as the carry.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.config import SYNTHETIC_SOURCE_PREFIX
from src.db.models import AnomalyEpisodeRecord, SensorReading
from src.db.upsert import bulk_upsert_rows
from src.models.fault_classifier import FaultType
from src.models.stuck_fault_detector import StuckCarry, expected_slot_count

# Upsert key: must match uq_anomaly_episodes_tenant_gh_channel_detector_onset
# (alembic/versions/026_add_anomaly_episodes.py) exactly.
_EPISODE_CONFLICT_COLS: tuple[str, ...] = (
    "tenant_id", "greenhouse_id", "channel", "detector", "onset_ts",
)
# Columns a re-upload / fault-continuation is allowed to overwrite in place.
# tenant_id/greenhouse_id/channel/detector/onset_ts/fault_type/id are not
# listed: the first four are the conflict key, fault_type is fixed for a
# given detector, and id must survive an update untouched.
_EPISODE_UPDATE_COLS: tuple[str, ...] = (
    "end_ts", "detected_ts", "release_ts", "duration_seconds",
    "frozen_sample_count", "raw_slot_count", "severity", "trigger_value",
    "is_ongoing", "source",
)

_VALID_FAULT_TYPES: frozenset[str] = frozenset(ft.value for ft in FaultType)

_DETECTOR_NAME = "stuck_fault"


def _parse_iso_utc(ts: str) -> datetime:
    """Parse an alert's ISO timestamp (possibly ``Z``-suffixed) to UTC."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _synthetic_source_for_window(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    start_dt: datetime,
    end_dt: datetime,
) -> str | None:
    """Earliest synthetic ``source`` marker among readings inside the window.

    Per task3_4_spec.md: ``None`` means every reading in
    ``[onset_ts, end_ts]`` is real; otherwise the earliest-by-timestamp
    synthetic marker wins when the window mixes several injected types.
    Matched on ``SensorReading.source`` (ADR-010) — never ``RawReading``'s
    column of the same name, which marks the originating sensor system, not
    synthetic provenance. The stored value is already the exact
    ``synthetic_source()`` string written at injection time, so it is read
    back verbatim rather than rebuilt.
    """
    stmt = (
        select(SensorReading.source)
        .where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
            SensorReading.timestamp >= start_dt,
            SensorReading.timestamp <= end_dt,
            SensorReading.source.like(f"{SYNTHETIC_SOURCE_PREFIX}%"),
        )
        .order_by(SensorReading.timestamp.asc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _build_episode_row(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    channel: str,
    onset_dt: datetime,
    end_dt: datetime,
    release_dt: datetime | None,
    frozen_sample_count: int | None,
    raw_slot_count: int | None,
    is_ongoing: bool,
    alert: UploadHighAnomalyDetail | None,
) -> dict[str, Any]:
    """Build one upsert row. Raises on any writer-validation failure."""
    if frozen_sample_count is None or raw_slot_count is None:
        raise ValueError(
            f"stuck_fault episode missing a sample count: channel={channel!r} "
            f"onset_ts={onset_dt.isoformat()!r} "
            f"frozen_sample_count={frozen_sample_count!r} "
            f"raw_slot_count={raw_slot_count!r} — both must be populated by "
            "the detector before this row can be persisted"
        )
    if _DETECTOR_NAME not in _VALID_FAULT_TYPES:
        raise ValueError(
            f"Invalid fault_type {_DETECTOR_NAME!r}; must be one of "
            f"{sorted(_VALID_FAULT_TYPES)}"
        )
    if alert is None:
        raise ValueError(
            f"No stuck_fault alert found for channel={channel!r} "
            f"onset_ts={onset_dt.isoformat()!r} — has_onset_fired is True but "
            "high_anomalies carries no matching alert; detected_ts/severity/"
            "trigger_value cannot be recovered"
        )

    detected_ts = (
        _parse_iso_utc(alert.detected_ts) if alert.detected_ts else end_dt
    )
    duration_seconds = int(round((end_dt - onset_dt).total_seconds()))
    source = await _synthetic_source_for_window(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        start_dt=onset_dt,
        end_dt=end_dt,
    )

    return {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
        "channel": channel,
        "detector": _DETECTOR_NAME,
        "fault_type": _DETECTOR_NAME,
        "onset_ts": onset_dt,
        "end_ts": end_dt,
        "detected_ts": detected_ts,
        "release_ts": release_dt,
        "duration_seconds": duration_seconds,
        "frozen_sample_count": frozen_sample_count,
        "raw_slot_count": raw_slot_count,
        "severity": alert.severity,
        "trigger_value": alert.value,
        "is_ongoing": is_ongoing,
        "source": source,
    }


async def persist_stuck_fault_episodes(
    db: AsyncSession,
    *,
    tenant_id: str,
    greenhouse_id: str,
    stuck_carry: StuckCarry | None,
    high_anomalies: list[UploadHighAnomalyDetail],
) -> int:
    """Upsert ``anomaly_episodes`` rows for this upload's stuck-fault freezes.

    Call after ``finalize_scores`` returns, passing its ``stuck_carry``
    (post-flush) and ``response.high_anomalies``. No-ops when there is no
    carry (stuck detection was not run). Returns the number of rows upserted.
    """
    if stuck_carry is None:
        return 0

    alerts_by_key: dict[tuple[str, str], UploadHighAnomalyDetail] = {
        (alert.column, alert.timestamp): alert
        for alert in high_anomalies
        if alert.detector == _DETECTOR_NAME and alert.column is not None
    }

    released = [fw for fw in stuck_carry.completed_faults if fw.has_fired]
    released_keys = {(fw.channel, fw.onset_ts) for fw in released}

    rows: list[dict[str, Any]] = []

    for fw in released:
        rows.append(
            await _build_episode_row(
                db,
                tenant_id=tenant_id,
                greenhouse_id=greenhouse_id,
                channel=fw.channel,
                onset_dt=fw.onset_dt,
                end_dt=fw.last_frozen_dt,
                release_dt=fw.release_dt,
                frozen_sample_count=fw.frozen_sample_count,
                raw_slot_count=fw.raw_slot_count,
                is_ongoing=False,
                alert=alerts_by_key.get((fw.channel, fw.onset_ts)),
            )
        )

    # Open freezes that crossed the gate but have not released. Deduped
    # against `released_keys`: a channel can hold a released window and a
    # new open state at once (upload_portions.py's already_completed check
    # follows the same shape).
    for channel, state in stuck_carry.channels.items():
        if not stuck_carry.has_onset_fired(channel, state.onset_ts):
            continue
        if (channel, state.onset_ts) in released_keys:
            continue
        rows.append(
            await _build_episode_row(
                db,
                tenant_id=tenant_id,
                greenhouse_id=greenhouse_id,
                channel=channel,
                onset_dt=state.onset_dt,
                end_dt=state.last_dt,
                release_dt=None,
                frozen_sample_count=state.sample_count,
                raw_slot_count=expected_slot_count(
                    state.onset_dt, state.last_dt, state.cadence_seconds,
                ),
                is_ongoing=True,
                alert=alerts_by_key.get((channel, state.onset_ts)),
            )
        )

    if not rows:
        return 0

    return await bulk_upsert_rows(
        db,
        AnomalyEpisodeRecord,
        rows,
        update_cols=_EPISODE_UPDATE_COLS,
        conflict_cols=_EPISODE_CONFLICT_COLS,
    )

"""Shadow drift-watch service: calibrate and score on the CSV upload path.

This module wires ``src.models.drift_watch.DriftWatch`` into both upload
paths (synchronous ``cold_run_upload`` and worker-side ``upload_consumer``)
in SHADOW mode.  Nothing it produces is exposed to operators, raises an
anomaly, or changes any existing API response.

Environment flag
----------------
``DRIFT_WATCH_SHADOW_ENABLED`` (default "1").  Set to "0", "false" or "no"
to disable entirely; a disabled call returns immediately without touching the
database.

Role mapping
------------
The drift watch is vendor-agnostic.  Raw column names stored in
``RawReading.data`` are already the cleaned/canonicalised keys produced by
``src.vendors.csv_adapter._clean_col_name`` + ``_canonicalize_raw_column``.
Roles are discovered by scanning those keys against the alias tuples from
``src.config``:

  - Greenhouse temperature: canonical "temperature" (always present)
  - Plant temperature:      PLANT_TEMP_COLUMN_ALIASES
  - Outdoor temperature:    OUTDOOR_TEMP_COLUMN_ALIASES
  - Heating setpoint:       SETPOINT_COLUMN_ALIASES
  - Measured humidity deficit: MEASURED_HD_COLUMN_ALIASES
  - RH:                     canonical "humidity" (always present)

No new alias lists are added.  If a role cannot be mapped with existing
aliases, that input type is skipped (reported in this module's docstring
above under "Role mapping").
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import (
    DRIFT_WATCH_BASELINE_WEEKS,
    DRIFT_WATCH_DEFAULT_SHIFT_HD_GM3,
    DRIFT_WATCH_DEFAULT_SHIFT_RH_PCT,
    DRIFT_WATCH_DEFAULT_SHIFT_TEMP_C,
    DRIFT_WATCH_MIN_HISTORY_WEEKS,
    DRIFT_WATCH_REFIT_INTERVAL_WEEKS,
    DRIFT_WATCH_SHADOW_ENABLED,
    MEASURED_HD_COLUMN_ALIASES,
    OUTDOOR_TEMP_COLUMN_ALIASES,
    PLANT_TEMP_COLUMN_ALIASES,
    SETPOINT_COLUMN_ALIASES,
)
from src.api.services.drift_watch_alerts import refresh_alert_state
from src.db.models import DriftWatchEpisode, DriftWatchInput, RawReading
from src.db.upsert import dialect_insert
from src.models.drift_watch import (
    CalibrationResult,
    DriftEpisode,
    DriftInputSpec,
    DriftInputState,
    DriftWatch,
    InputKind,
)

logger = logging.getLogger(__name__)

# Canonical channel names always present in RawReading.data for a normalised upload.
_CANONICAL_TEMP = "temperature"
_CANONICAL_RH = "humidity"
_CANONICAL_CO2 = "co2"
_CANONICAL_LIGHT = "light"

# Maximum lookback when querying for calibration history (2 years).
_CALIBRATION_MAX_LOOKBACK_WEEKS = 104


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_specs(available_columns: set[str]) -> list[DriftInputSpec]:
    """Build drift-input specs from the set of available column names.

    Rules:
    - ``temp_paired``: paired_probes when greenhouse-temp AND a plant-temp
      column are both present; outdoor-temp and setpoint added as covariates
      when available.
    - ``rh_virtual``: virtual_sensor when RH is present and at least two
      other usable predictors are available.  Measured humidity deficit is
      never used as a predictor of RH.
    - ``hd_physics``: physics when temperature, RH and a measured-HD column
      are all present.

    Args:
        available_columns: Set of cleaned column keys from ``RawReading.data``.

    Returns:
        List of ``DriftInputSpec`` objects (may be empty).
    """
    specs: list[DriftInputSpec] = []

    # -- 1. Paired probes: greenhouse temp vs plant temp ----------------------
    plant_col = _first_alias(PLANT_TEMP_COLUMN_ALIASES, available_columns)
    if plant_col is not None and _CANONICAL_TEMP in available_columns:
        covariates: list[str] = []
        outdoor_col = _first_alias(OUTDOOR_TEMP_COLUMN_ALIASES, available_columns)
        if outdoor_col:
            covariates.append(outdoor_col)
        setpoint_col = _first_alias(SETPOINT_COLUMN_ALIASES, available_columns)
        if setpoint_col:
            covariates.append(setpoint_col)
        specs.append(
            DriftInputSpec(
                input_id="temp_paired",
                kind=InputKind.PAIRED_PROBES,
                target_channel=_CANONICAL_TEMP,
                channels=[plant_col] + covariates,
                min_shift=DRIFT_WATCH_DEFAULT_SHIFT_TEMP_C,
            )
        )

    # -- 2. Virtual sensor: RH from other channels (never from measured HD) --
    if _CANONICAL_RH in available_columns:
        hd_col_excl = _first_alias(MEASURED_HD_COLUMN_ALIASES, available_columns)
        # Candidate predictors: canonical channels + optional extras.
        predictor_pool = [_CANONICAL_TEMP, _CANONICAL_CO2, _CANONICAL_LIGHT]
        outdoor_col_v = _first_alias(OUTDOOR_TEMP_COLUMN_ALIASES, available_columns)
        if outdoor_col_v:
            predictor_pool.append(outdoor_col_v)
        setpoint_col_v = _first_alias(SETPOINT_COLUMN_ALIASES, available_columns)
        if setpoint_col_v:
            predictor_pool.append(setpoint_col_v)
        # Keep only columns that are present and not the target; exclude HD.
        predictors = [
            c
            for c in predictor_pool
            if c in available_columns
            and c != _CANONICAL_RH
            and c != hd_col_excl
        ]
        if len(predictors) >= 2:
            specs.append(
                DriftInputSpec(
                    input_id="rh_virtual",
                    kind=InputKind.VIRTUAL_SENSOR,
                    target_channel=_CANONICAL_RH,
                    channels=predictors,
                    min_shift=DRIFT_WATCH_DEFAULT_SHIFT_RH_PCT,
                )
            )

    # -- 3. Physics: HD residual vs Tetens(T, RH) ----------------------------
    hd_col = _first_alias(MEASURED_HD_COLUMN_ALIASES, available_columns)
    if (
        hd_col is not None
        and _CANONICAL_TEMP in available_columns
        and _CANONICAL_RH in available_columns
    ):
        specs.append(
            DriftInputSpec(
                input_id="hd_physics",
                kind=InputKind.PHYSICS,
                target_channel=hd_col,
                channels=[_CANONICAL_TEMP, _CANONICAL_RH, hd_col],
                min_shift=DRIFT_WATCH_DEFAULT_SHIFT_HD_GM3,
            )
        )

    return specs


async def load_hourly_frame(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    since: datetime,
    until: datetime,
    columns: list[str],
) -> pd.DataFrame:
    """Load an hourly-mean DataFrame from ``RawReading.data`` for the given columns.

    Excludes rows whose ``source`` starts with ``"synthetic:"`` (the same
    filter used by ``_query_physical_history`` in ``comprehensive_pipeline``).
    Non-numeric values and ``NaN``/``inf`` are coerced to ``NaN`` and left
    to ``resample().mean()`` to skip.

    Args:
        db: Async database session.
        tenant_id: Tenant identifier.
        greenhouse_id: Greenhouse identifier.
        since: Lower bound (inclusive) on ``RawReading.timestamp`` (UTC).
        until: Upper bound (exclusive) on ``RawReading.timestamp`` (UTC).
        columns: Column names to extract from the ``data`` JSON blob.

    Returns:
        ``pd.DataFrame`` indexed by UTC timestamps at hourly cadence with
        one column per requested column plus an ``hour`` column (0–23).
        Returns an empty ``DataFrame`` if no rows are found.
    """
    stmt = (
        select(RawReading.timestamp, RawReading.data)
        .where(
            RawReading.tenant_id == tenant_id,
            RawReading.greenhouse_id == greenhouse_id,
            RawReading.timestamp >= since,
            RawReading.timestamp < until,
            or_(
                RawReading.source.is_(None),
                ~RawReading.source.startswith("synthetic:"),
            ),
        )
        .order_by(RawReading.timestamp)
    )
    result = await db.execute(stmt)
    rows = result.all()

    if not rows:
        return pd.DataFrame()

    records: list[dict[str, Any]] = []
    for ts, data in rows:
        if data is None:
            continue
        record: dict[str, Any] = {"timestamp": ts}
        for col in columns:
            val = data.get(col)
            record[col] = _to_float_or_nan(val)
        records.append(record)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()

    # Resample to hourly means; non-numeric columns are already float/NaN.
    hourly = df.resample("1h").mean()
    hourly["hour"] = hourly.index.hour
    return hourly


async def run_drift_watch_shadow(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    upload_start: datetime,
    upload_end: datetime,
) -> None:
    """Calibrate / update drift-watch inputs in SHADOW mode after an upload.

    Does nothing if ``DRIFT_WATCH_SHADOW_ENABLED`` is false or if
    ``upload_start``/``upload_end`` are invalid.  Any internal failure is
    caught, logged at WARNING level, and swallowed — this function must never
    propagate an exception to the calling upload path.

    Args:
        db: Async database session (must be active; caller owns the commit).
        tenant_id: Tenant identifier.
        greenhouse_id: Greenhouse identifier.
        upload_start: Earliest timestamp in the uploaded batch (UTC).
        upload_end: Latest timestamp in the uploaded batch (UTC).
    """
    if not DRIFT_WATCH_SHADOW_ENABLED:
        return
    if upload_start is None or upload_end is None:
        return
    try:
        await _run_shadow(db, tenant_id, greenhouse_id, upload_start, upload_end)
    except Exception:
        logger.warning(
            "drift_watch shadow failed for tenant=%s greenhouse=%s — upload unaffected",
            tenant_id,
            greenhouse_id,
            exc_info=True,
        )


async def backfill_drift_watch(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    *,
    step_weeks: int = DRIFT_WATCH_REFIT_INTERVAL_WEEKS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Calibrate and score drift watch for all stored raw readings in a scope.

    Designed for tenants whose data was uploaded before the drift-watch feature
    existed.  Walks the full raw-readings time span in ``step_weeks``-wide
    chunks (default: ``DRIFT_WATCH_REFIT_INTERVAL_WEEKS`` = 1 week), calling
    the same code path that a live upload triggers so calibration happens once
    enough history exists and later chunks record episodes exactly as
    consecutive uploads would.

    Idempotency guarantee: existing ``DriftWatchInput`` and
    ``DriftWatchEpisode`` rows for the scope are deleted before the walk
    begins, so a second run always starts from a clean state and produces the
    same final rows.  This is a full reset, not an incremental update; callers
    should not run this alongside active uploads for the same scope.

    Synthetic rows (``source`` starting with ``"synthetic:"``) are excluded
    from the time-span query and from every calibration/scoring frame, matching
    the filter used by the existing shadow service.

    Args:
        db: Async database session.  The function commits internally (once per
            chunk plus an initial cleanup commit).  Callers should not hold
            unflushed writes in the same session.
        tenant_id: Tenant identifier.
        greenhouse_id: Greenhouse identifier.
        step_weeks: Chunk width in weeks.  Defaults to
            ``DRIFT_WATCH_REFIT_INTERVAL_WEEKS`` (1 week).
        dry_run: If ``True``, report the span and specs that would be built
            but write nothing to the database.

    Returns:
        A dict with keys:
        - ``dry_run`` (bool)
        - ``data_start`` / ``data_end`` (ISO-8601 strings, or ``None`` if no data)
        - ``data_span_weeks`` (float)
        - ``specs_built`` / ``specs_would_be_built`` (list[str] of input_ids)
        - ``chunks_processed`` / ``chunks_would_be_processed`` (int)
        - ``episodes_recorded`` (int; always 0 for dry_run)
        - ``inputs_status`` (dict[str, str]; empty for dry_run)
        - ``elapsed_seconds`` (float)
    """
    t0 = time.monotonic()

    # -- 1. Find the time span of non-synthetic raw readings ------------------
    span_stmt = select(
        func.min(RawReading.timestamp),
        func.max(RawReading.timestamp),
    ).where(
        RawReading.tenant_id == tenant_id,
        RawReading.greenhouse_id == greenhouse_id,
        or_(
            RawReading.source.is_(None),
            ~RawReading.source.startswith("synthetic:"),
        ),
    )
    span_result = await db.execute(span_stmt)
    data_start_raw, data_end_raw = span_result.one()

    if data_start_raw is None or data_end_raw is None:
        elapsed = time.monotonic() - t0
        return {
            "dry_run": dry_run,
            "data_start": None,
            "data_end": None,
            "data_span_weeks": 0.0,
            "specs_built": [] if not dry_run else None,
            "specs_would_be_built": [] if dry_run else None,
            "chunks_processed": 0 if not dry_run else None,
            "chunks_would_be_processed": 0 if dry_run else None,
            "episodes_recorded": 0,
            "inputs_status": {},
            "elapsed_seconds": elapsed,
        }

    data_start: datetime = _ensure_utc(data_start_raw)
    data_end: datetime = _ensure_utc(data_end_raw)
    span_weeks = (data_end - data_start).total_seconds() / (7 * 24 * 3600)

    # -- 2. Discover available columns and build specs ------------------------
    available = await _discover_available_columns(db, tenant_id, greenhouse_id)
    specs = build_specs(available) if available else []

    # -- 3. Count chunks ------------------------------------------------------
    chunk_count = 0
    chunk_start = data_start
    while chunk_start <= data_end:
        chunk_count += 1
        chunk_start = chunk_start + timedelta(weeks=step_weeks)

    if dry_run:
        elapsed = time.monotonic() - t0
        return {
            "dry_run": True,
            "data_start": data_start.isoformat(),
            "data_end": data_end.isoformat(),
            "data_span_weeks": round(span_weeks, 2),
            "specs_would_be_built": [s.input_id for s in specs],
            "chunks_would_be_processed": chunk_count,
            "episodes_recorded": 0,
            "inputs_status": {},
            "elapsed_seconds": time.monotonic() - t0,
        }

    # -- 4a. Remember each input's alert state (the rows are recreated) -------
    prior_alert: dict[str, tuple[str, Optional[str], Optional[datetime]]] = {
        r.input_id: (r.alert_state, r.alert_state_reason, r.alert_state_changed_at)
        for r in (
            await db.execute(
                select(DriftWatchInput).where(
                    DriftWatchInput.tenant_id == tenant_id,
                    DriftWatchInput.greenhouse_id == greenhouse_id,
                )
            )
        ).scalars().all()
    }

    # -- 4. Reset: delete existing state for this scope -----------------------
    # drift_watch_feedback is deliberately NOT deleted: labels are keyed on the
    # episode natural key (ADR-013 §4) so they survive this reset.
    # Deleting both tables and committing before the walk ensures that a second
    # run starts from a clean state and produces identical results.
    await db.execute(
        delete(DriftWatchEpisode).where(
            DriftWatchEpisode.tenant_id == tenant_id,
            DriftWatchEpisode.greenhouse_id == greenhouse_id,
        )
    )
    await db.execute(
        delete(DriftWatchInput).where(
            DriftWatchInput.tenant_id == tenant_id,
            DriftWatchInput.greenhouse_id == greenhouse_id,
        )
    )
    await db.commit()

    # -- 5. Walk forward in step_weeks chunks ---------------------------------
    chunks_processed = 0
    chunk_start = data_start
    while chunk_start <= data_end:
        chunk_end = chunk_start + timedelta(weeks=step_weeks)
        logger.debug(
            "backfill_drift_watch: chunk %d [%s, %s) for %s/%s",
            chunks_processed + 1,
            chunk_start.isoformat(),
            chunk_end.isoformat(),
            tenant_id,
            greenhouse_id,
        )
        # Backfill writes no alerts and evaluates the gate once, below.
        await _run_shadow(
            db, tenant_id, greenhouse_id, chunk_start, chunk_end,
            refresh_gate=False,
        )
        chunks_processed += 1
        chunk_start = chunk_end

    # -- 5b. Recompute alert_state once, from the prior state -----------------
    # No alerts are written here (write_alerts=False); the next upload writes
    # any that qualify.
    for row in (
        await db.execute(
            select(DriftWatchInput).where(
                DriftWatchInput.tenant_id == tenant_id,
                DriftWatchInput.greenhouse_id == greenhouse_id,
            )
        )
    ).scalars().all():
        prior = prior_alert.get(row.input_id)
        if prior is not None:
            row.alert_state, row.alert_state_reason, row.alert_state_changed_at = prior
        if row.state_json is None:
            continue
        spec_d = json.loads(row.spec_json)
        spec = DriftInputSpec(
            input_id=spec_d["input_id"],
            kind=InputKind(spec_d["kind"]),
            target_channel=spec_d["target_channel"],
            channels=list(spec_d["channels"]),
            min_shift=float(spec_d["min_shift"]),
        )
        last_ts = json.loads(row.state_json).get("last_processed_ts")
        await refresh_alert_state(
            db, tenant_id, greenhouse_id, spec, row,
            as_of_ts=last_ts, write_alerts=False,
        )
    await db.commit()

    # -- 6. Collect final state -----------------------------------------------
    inputs_result = await db.execute(
        select(DriftWatchInput.input_id, DriftWatchInput.status).where(
            DriftWatchInput.tenant_id == tenant_id,
            DriftWatchInput.greenhouse_id == greenhouse_id,
        )
    )
    inputs_status = {row.input_id: row.status for row in inputs_result.all()}

    ep_count_result = await db.execute(
        select(func.count()).where(
            DriftWatchEpisode.tenant_id == tenant_id,
            DriftWatchEpisode.greenhouse_id == greenhouse_id,
        )
    )
    episodes_recorded: int = ep_count_result.scalar_one()

    elapsed = time.monotonic() - t0
    return {
        "dry_run": False,
        "data_start": data_start.isoformat(),
        "data_end": data_end.isoformat(),
        "data_span_weeks": round(span_weeks, 2),
        "specs_built": [s.input_id for s in specs],
        "chunks_processed": chunks_processed,
        "episodes_recorded": episodes_recorded,
        "inputs_status": inputs_status,
        "elapsed_seconds": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _first_alias(aliases: tuple[str, ...], available: set[str]) -> Optional[str]:
    """Return the first alias present in *available*, or ``None``."""
    for a in aliases:
        if a in available:
            return a
    return None


def _to_float_or_nan(val: Any) -> float:
    """Convert *val* to float; return ``float("nan")`` on failure or non-finite."""
    if val is None:
        return float("nan")
    try:
        f = float(val)
        return f if math.isfinite(f) else float("nan")  # exclude ±inf and NaN
    except (TypeError, ValueError):
        return float("nan")


def _spec_columns(spec: DriftInputSpec) -> list[str]:
    """All columns the spec needs present in the hourly DataFrame."""
    cols: set[str] = {spec.target_channel}
    cols.update(spec.channels)
    return list(cols)


def _ensure_utc(dt: datetime) -> datetime:
    """Return *dt* as a timezone-aware UTC datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _discover_available_columns(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
) -> set[str]:
    """Collect all column keys seen across a sample of recent raw readings."""
    stmt = (
        select(RawReading.data)
        .where(
            RawReading.tenant_id == tenant_id,
            RawReading.greenhouse_id == greenhouse_id,
        )
        .order_by(RawReading.timestamp.desc())
        .limit(200)
    )
    result = await db.execute(stmt)
    available: set[str] = set()
    for (data,) in result.all():
        if data:
            available.update(data.keys())
    return available


async def _get_or_create_input(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    spec: DriftInputSpec,
) -> DriftWatchInput:
    """Load or INSERT the drift-watch input row for *spec*."""
    stmt = select(DriftWatchInput).where(
        DriftWatchInput.tenant_id == tenant_id,
        DriftWatchInput.greenhouse_id == greenhouse_id,
        DriftWatchInput.input_id == spec.input_id,
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is not None:
        return row
    spec_dict = {
        "input_id": spec.input_id,
        "kind": spec.kind.value,
        "target_channel": spec.target_channel,
        "channels": spec.channels,
        "min_shift": spec.min_shift,
    }
    row = DriftWatchInput(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        input_id=spec.input_id,
        spec_json=json.dumps(spec_dict),
        calibration_json=None,
        state_json=None,
        status="uncalibrated",
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    return row


async def _upsert_episode(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    ep: DriftEpisode,
    input_status: str,
) -> None:
    """INSERT or UPDATE a drift episode row (idempotent on episode_id)."""
    now = datetime.now(timezone.utc)

    def _ts(t: pd.Timestamp) -> datetime:
        return t.to_pydatetime().replace(tzinfo=timezone.utc)

    insert_fn = dialect_insert(db)
    table = DriftWatchEpisode.__table__

    values: dict[str, Any] = {
        "tenant_id": tenant_id,
        "greenhouse_id": greenhouse_id,
        "input_id": ep.input_id,
        "episode_id": ep.episode_id,
        "target_channel": ep.target_channel,
        "direction": ep.direction,
        "first_detected_ts": _ts(ep.first_detected_ts),
        "last_confirmed_ts": _ts(ep.last_confirmed_ts),
        "onset_estimate_ts": _ts(ep.onset_estimate) if ep.onset_estimate is not None else None,
        "offset_estimate": float(ep.current_offset_native) if ep.current_offset_native is not None else None,
        "peak_statistic": float(ep.peak_statistic) if ep.peak_statistic is not None else None,
        "offset_robust": float(ep.offset_robust) if ep.offset_robust is not None else None,
        "input_status_at_detection": input_status,
        "created_at": now,
        "updated_at": now,
    }

    stmt = insert_fn(table).values(**values).on_conflict_do_update(
        index_elements=["tenant_id", "greenhouse_id", "input_id", "episode_id"],
        set_={
            "last_confirmed_ts": values["last_confirmed_ts"],
            "offset_estimate": values["offset_estimate"],
            "peak_statistic": values["peak_statistic"],
            "offset_robust": values["offset_robust"],
            "updated_at": now,
        },
    )
    await db.execute(stmt)


async def _score_new_data(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    spec: DriftInputSpec,
    input_row: "DriftWatchInput",
    state: DriftInputState,
    upload_end: datetime,
    need_cols: list[str],
    refresh_gate: bool = True,
) -> None:
    """Load update frame and run CUSUM scoring; persist state and episodes.

    With ``refresh_gate`` the ADR-013 alert gate is re-evaluated afterwards
    (and, for an ``enabled`` input with the global flag on, alerts written).
    ``backfill_drift_watch`` passes False and evaluates once at the end.

    Called both immediately after calibration (scoring the current upload's
    data for the first time) and on the update path for subsequent uploads.
    """
    context_since = upload_end - timedelta(weeks=DRIFT_WATCH_BASELINE_WEEKS + 2)
    update_frame = await load_hourly_frame(
        db, tenant_id, greenhouse_id,
        context_since, upload_end,
        need_cols,
    )
    if update_frame.empty:
        return

    episodes = DriftWatch.update(state, update_frame, spec)

    input_row.state_json = json.dumps(state.to_dict())
    input_row.updated_at = datetime.now(timezone.utc)
    await db.flush()

    for ep in episodes:
        await _upsert_episode(db, tenant_id, greenhouse_id, ep, input_row.status)
    await db.flush()

    if refresh_gate:
        await refresh_alert_state(
            db, tenant_id, greenhouse_id, spec, input_row,
            as_of_ts=state.last_processed_ts,
        )


async def _run_shadow(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    upload_start: datetime,
    upload_end: datetime,
    refresh_gate: bool = True,
) -> None:
    """Inner (non-guarded) implementation of :func:`run_drift_watch_shadow`."""
    upload_start = _ensure_utc(upload_start)
    upload_end = _ensure_utc(upload_end)

    available = await _discover_available_columns(db, tenant_id, greenhouse_id)
    if not available:
        return

    specs = build_specs(available)
    if not specs:
        return

    for spec in specs:
        input_row = await _get_or_create_input(db, tenant_id, greenhouse_id, spec)
        need_cols = _spec_columns(spec)

        if input_row.calibration_json is None:
            # --- Calibration path ---
            # Require MIN_HISTORY_WEEKS of data strictly BEFORE this upload's start.
            # In production: prior uploads supply this history.  On first ever
            # upload there is no prior data; skip silently.
            cal_since = upload_start - timedelta(
                weeks=_CALIBRATION_MAX_LOOKBACK_WEEKS
            )
            cal_frame = await load_hourly_frame(
                db, tenant_id, greenhouse_id,
                cal_since, upload_start,
                need_cols,
            )
            if cal_frame.empty:
                continue

            total_hours = len(cal_frame)
            min_hours = int(DRIFT_WATCH_MIN_HISTORY_WEEKS * 7 * 24 * 0.75)
            if total_hours < min_hours:
                logger.debug(
                    "drift_watch calibration skipped for %s/%s/%s: "
                    "%d hourly points < %d required",
                    tenant_id, greenhouse_id, spec.input_id,
                    total_hours, min_hours,
                )
                continue

            try:
                cal_result = DriftWatch.calibrate(cal_frame, spec)
            except ValueError as exc:
                logger.debug(
                    "drift_watch calibrate raised for %s/%s/%s: %s",
                    tenant_id, greenhouse_id, spec.input_id, exc,
                )
                continue

            state = DriftWatch.init_state(cal_result)
            input_row.calibration_json = json.dumps(cal_result.to_dict())
            input_row.state_json = json.dumps(state.to_dict())
            input_row.status = cal_result.status
            input_row.updated_at = datetime.now(timezone.utc)
            await db.flush()

            # Score the current upload immediately after calibration so that
            # the second upload (which triggers calibration) also yields
            # episodes from its own data.
            await _score_new_data(
                db, tenant_id, greenhouse_id, spec, input_row, state,
                upload_end, need_cols, refresh_gate,
            )

        else:
            # --- Update path ---
            state = DriftInputState.from_dict(json.loads(input_row.state_json))  # type: ignore[arg-type]
            await _score_new_data(
                db, tenant_id, greenhouse_id, spec, input_row, state,
                upload_end, need_cols, refresh_gate,
            )

    # Commit all flushed rows.  In production the calling upload path's
    # get_db dependency also commits; the extra commit here is harmless and
    # ensures the data survives in contexts (e.g. tests) where the caller
    # does not commit.
    await db.commit()

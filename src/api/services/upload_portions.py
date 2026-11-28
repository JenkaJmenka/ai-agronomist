"""Per-portion CSV scoring core.

Shared by the synchronous upload path (run_cold_run_upload drives it chunk by
chunk) and the RabbitMQ upload worker (one portion per message). Memory is
bounded per portion: only anomaly scores, sequence timestamps and sparse
statistical hits survive a portion; full raw rows and embeddings do not
(embeddings are retained only when a caller opts in).
"""

from __future__ import annotations

import dataclasses
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from src.api.schemas.sensor_data import (
    AnomalyEpisodeSchema,
    StatisticalAnomalyDetail,
    StatisticalColdRunSummary,
    UploadHighAnomalyDetail,
    UploadInferenceResponse,
)
from src.api.services.score_transform import canonical as _canonical_transform
from src.config import (
    ANOMALY_SCALING_FACTOR,
    COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS,
    COLD_RUN_STAT_ROLLING_WINDOW,
    COLD_RUN_STAT_Z_MIN,
    LATENT_DIM,
)
from src.models.psychrometric_validator import validate_row as _psychro_validate_row
from src.models.cold_run_detector import RollingAnomaly, _effective_rolling_window
from src.models.cold_run_threshold import calibrate_threshold
from src.models.data_utils import normalize_data, preprocess_sequences
from src.models.inference import AnomalyDetector
from src.models.episode_grouper import group_alerts_into_episodes
from src.models.scoring import BATCH_PROFILE, ScoringContext
from src.models.scoring.stream_stages import (
    RowHit,
    UploadStreamState,
    clamp_frame,
    flush_duration_stuck,
    score_upload_stream,
)
from src.models.stuck_fault_detector import StuckCarry

# ---------------------------------------------------------------------------
# Exact canonical column names for the air-temperature and relative-humidity
# sensors as produced by csv_adapter.parse_raw_rows (via _RAW_COLUMN_ALIASES
# and _canonicalize_raw_column).  These match REQUIRED_SENSOR_COLS[0] and [1]
# in src/vendors/csv_adapter.py.
#
# These are the ONLY columns that participate in:
#   - Psychrometric pair suppression (_suppress_psychrometric_pairs)
#   - Alert suppression window matching (_suppress_fault_window_hits)
#   - Baseline exclusion matching (stream_stages.exclude_frozen_readings)
#
# Other temperature-like columns (measured_plant_temp, outdoor_temp, pipe
# temps) are NOT air-temperature partners and are never matched by these
# constants.
# ---------------------------------------------------------------------------
AIR_TEMPERATURE_COLUMN: str = "temperature"
RELATIVE_HUMIDITY_COLUMN: str = "humidity"


def _stat_z_to_canonical(z_score: float) -> float:
    """Map a statistical z-score to the ADR-005 canonical [0, 1] scale.

    Decision point p = COLD_RUN_STAT_Z_MIN (config default 5.5):
      z == p  → canonical 0.5  (detector's own threshold)
      z > p   → asymptotes toward 1.0 as z grows
      z < p   → [0, 0.5) (below threshold, not normally reached here)

    Uses the unbounded branch (hi=None) of score_transform.canonical so
    the score never saturates at 1.0 — z must be infinite to reach it.
    """
    can = _canonical_transform(z_score, COLD_RUN_STAT_Z_MIN, None)
    return can if can is not None else 0.5


def _lstm_score_to_canonical(score: float, threshold_used: float) -> float:
    """Map a clipped LSTM reconstruction score to the canonical [0, 1] scale.

    Decision point p = the file-calibrated ``threshold_used`` (the LSTM's own
    cutoff for this upload), unbounded branch, same as the statistical mapping.

    Falls back to 0.5 — the decision point — when the transform is undefined
    (``threshold_used <= 0``, reachable when >99.9% of a file scores zero).
    Callers only build these for scores already above the threshold, so the
    decision point is the honest floor.
    """
    can = _canonical_transform(score, threshold_used, None)
    return can if can is not None else 0.5

SENSOR_COLS = ["temperature", "humidity", "co2", "light"]

# The batch stream stages read no tenant, clock or history (ADR-014 A7), so the
# upload path passes one fixed context rather than building one per portion.
_UPLOAD_CONTEXT = ScoringContext(
    tenant_id="upload",
    greenhouse_id="upload",
    sequence_timestamp=datetime(1970, 1, 1),
    profile=BATCH_PROFILE,
)


@dataclass
class StatCarry:
    """Rolling state threaded between portions for statistical detection."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    seen: set[tuple[str, str]] = field(default_factory=set)
    total_rows: int = 0
    columns: list[str] | None = None
    # Psychrometric context: {timestamp: (T_air, RH, measured_hd)} for
    # timestamps with stat anomalies where all three values are present.
    # Used by finalize_scores to suppress physically-expected pairings.
    psychro_context: dict[str, tuple[float, float, float]] = field(
        default_factory=dict
    )


@dataclass
class PortionScores:
    """Compact per-portion inference output (safe to serialize for MQ jobs)."""

    scores: list[float] = field(default_factory=list)
    sequence_timestamps: list[str] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    uncertainties: list[float] = field(default_factory=list)
    stat_hits: list[StatisticalAnomalyDetail] = field(default_factory=list)
    stuck_hits: list[UploadHighAnomalyDetail] = field(default_factory=list)
    mad_hits: list[UploadHighAnomalyDetail] = field(default_factory=list)
    oob_hits: list[UploadHighAnomalyDetail] = field(default_factory=list)
    rows_processed: int = 0


def _timestamp_label(value: Any) -> str:
    """ISO-8601 when the cell is a datetime; otherwise its string form."""
    if value is None:
        return "unknown"
    if isinstance(value, float) and math.isnan(value):
        return "unknown"
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _calibrate_upload_threshold(scores: np.ndarray) -> tuple[float, str]:
    """File threshold for one upload.

    Returns ``calibrate_threshold`` unchanged. The robust gate follows that
    function: it is off unless ``COLD_RUN_ROBUST_GATE_ENABLED`` enables it;
    when enabled it can only raise the cutoff.
    """
    return calibrate_threshold(scores)


def _sequence_timestamps_for(combined_df: pd.DataFrame, n_scores: int, seq_len: int) -> list[str]:
    timestamps = combined_df["timestamp"].tolist()
    out: list[str] = []
    for idx in range(n_scores):
        row_idx = idx + seq_len - 1
        if row_idx < len(timestamps) and not pd.isnull(timestamps[row_idx]):
            ts_val = timestamps[row_idx]
            out.append(ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val))
        else:
            out.append("unknown")
    return out


def _stuck_dict_to_high_detail(
    alert: dict[str, Any],
) -> UploadHighAnomalyDetail:
    """Convert a ``WallclockStuckDetector`` alert dict to the upload schema.

    ``detected_ts`` carries the timestamp at which detection actually fired
    (when the gate was first satisfied), distinct from ``timestamp`` which
    is the freeze onset.  Latency = ``detected_ts`` - ``timestamp``.
    """
    return UploadHighAnomalyDetail(
        score=1.0,
        timestamp=alert["timestamp"],
        detected_ts=alert.get("detected_ts"),
        detector=alert["detector"],
        column=alert["channel"],
        severity=alert["severity"],
        method=alert["method"],
        # Stuck hits carry hours-at-detection in frozen_hours_at_detection, not
        # z_score (there is no statistical z for a frozen channel).
        z_score=None,
        frozen_hours_at_detection=alert["duration_hours"],
        value=alert["value"],
    )


def _is_air_temp_stat_column(col: str) -> bool:
    """True when *col* is the exact canonical air-temperature column.

    Only the ``"temperature"`` column — the greenhouse/compartment
    air-temperature sensor as canonicalised by
    ``csv_adapter.parse_raw_rows`` — is a psychrometric partner.

    Other temperature-like columns (``measured_plant_temp``,
    ``outdoor_temp``, pipe temps, ``calculated_heating_temp``) are
    NOT air-temperature partners and return ``False``.
    """
    return col == AIR_TEMPERATURE_COLUMN


def _is_humidity_stat_column(col: str) -> bool:
    """True when *col* is the exact canonical relative-humidity column.

    Only the ``"humidity"`` column — the RH sensor as canonicalised by
    ``csv_adapter.parse_raw_rows`` — is a psychrometric partner.

    Humidity deficit (``measured_hd``) is a derived quantity and returns
    ``False``.
    """
    return col == RELATIVE_HUMIDITY_COLUMN


def _suppress_psychrometric_pairs(
    stat_details: list[StatisticalAnomalyDetail],
    psychro_context: dict[str, tuple[float, float, float]],
) -> list[StatisticalAnomalyDetail]:
    """Suppress paired temp+humidity alerts that are psychrometrically consistent.

    When air temperature drops and relative humidity rises simultaneously, the
    Tetens equation predicts exactly that coupling.  If the measured humidity
    deficit (HD) AGREES with the HD derived from T and RH, the pairing is
    expected physics — suppress both sides.  If HD DISAGREES, the sensors
    contradict thermodynamics — keep the original alerts and add a
    ``psychrometric_inconsistency`` marker so the fault is surfaced.

    When measured_hd is absent for a timestamp, the check does not run and
    behaviour is identical to pre-suppression (no silent default, no
    fabricated HD).
    """
    if not psychro_context:
        return stat_details

    by_ts: dict[str, list[StatisticalAnomalyDetail]] = defaultdict(list)
    for detail in stat_details:
        by_ts[detail.timestamp].append(detail)

    suppressed_ts: set[str] = set()
    inconsistent_extras: list[StatisticalAnomalyDetail] = []

    for ts, details in by_ts.items():
        has_temp = any(_is_air_temp_stat_column(d.column) for d in details)
        has_humid = any(_is_humidity_stat_column(d.column) for d in details)
        if not (has_temp and has_humid):
            continue

        ctx = psychro_context.get(ts)
        if ctx is None:
            continue

        t_air, rh, hd_measured = ctx
        try:
            status = _psychro_validate_row(t_air, rh, hd_measured)
        except ValueError:
            # Unphysical inputs (e.g. RH > 100 from sensor noise) — skip.
            continue

        if status == "OK":
            # Psychrometrically consistent: suppress paired alerts.
            suppressed_ts.add(ts)
        else:
            # Sensor disagrees with physics — flag the inconsistency.
            inconsistent_extras.append(
                StatisticalAnomalyDetail(
                    timestamp=ts,
                    column="psychrometric",
                    value=hd_measured,
                    z_score=0.0,
                    severity="warning",
                    method="psychrometric_inconsistency",
                )
            )

    if not suppressed_ts and not inconsistent_extras:
        return stat_details

    result = [
        d for d in stat_details
        if not (
            d.timestamp in suppressed_ts
            and (_is_air_temp_stat_column(d.column) or _is_humidity_stat_column(d.column))
        )
    ]
    result.extend(inconsistent_extras)
    return result


def _parse_iso_utc(ts: str) -> datetime:
    """Parse an ISO timestamp to a timezone-aware UTC datetime.

    Handles both tz-aware strings ("2026-12-14T00:00:00+00:00") and
    naive strings ("2026-12-14T00:00:00") — the latter is treated as UTC.
    """
    from datetime import datetime as _dt, timezone as _tz
    dt = _dt.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_tz.utc)
    return dt.astimezone(_tz.utc)


def _suppress_fault_window_hits(
    stat_details: list[StatisticalAnomalyDetail],
    stuck_carry: StuckCarry | None,
) -> list[StatisticalAnomalyDetail]:
    """Suppress statistical hits on faulted channels within suppression windows.

    Two rules, side by side:

    **Alert suppression** (this function):
        Window ``[onset_ts, release_ts + N]`` where
        ``N = COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS`` (6 h).
        Statistical hits on the FAULTED CHANNEL ONLY are dropped.
        For an open freeze that crossed the gate (no release yet),
        the window is ``[onset_ts, +inf)``.
        Applied BEFORE pair suppression.

    **Baseline exclusion** (separate, applied per-portion in the
    ``stream_stages.rolling_z_stage``):
        Window ``[onset_ts, last_frozen_ts]``.
        Frozen values on the faulted channel are NaN-ed before rolling
        statistics are computed.  The release sample is a genuine reading
        and stays in the baseline.

    Channel matching is by **exact column name** — a hit is suppressed
    only when ``hit.column`` equals the stuck-detector's channel name
    (e.g. ``"humidity"``).  Other columns that map to the same sensor
    *slot* in the dashboard enricher (e.g. ``measured_plant_temp`` for
    ``"temperature"``) are NOT suppressed.
    """
    if stuck_carry is None:
        return stat_details
    from datetime import timedelta

    suppress_hours = COLD_RUN_STAT_POST_RELEASE_SUPPRESS_HOURS

    # Build suppression windows from completed faults + open emitted faults.
    # Each window: (canonical_channel, onset_dt, end_dt)
    windows: list[tuple[str, datetime, datetime | None]] = []

    # Completed faults: [onset_dt, release_dt + N]
    for fw in stuck_carry.completed_faults:
        suppress_end = fw.release_dt + timedelta(hours=suppress_hours)
        windows.append((fw.channel, fw.onset_dt, suppress_end))

    # Open emitted faults: [onset_dt, +inf)
    for ch, state in stuck_carry.channels.items():
        # Matched by instant via has_onset_fired, not by spelling against
        # emitted_onsets directly -- see StuckCarry.has_onset_fired.
        if stuck_carry.has_onset_fired(ch, state.onset_ts):
            # Check this isn't already in completed_faults
            already_completed = any(
                fw.channel == ch and fw.onset_ts == state.onset_ts
                for fw in stuck_carry.completed_faults
            )
            if not already_completed:
                windows.append((ch, state.onset_dt, None))  # None = +inf

    if not windows:
        return stat_details

    result: list[StatisticalAnomalyDetail] = []
    for hit in stat_details:
        # Parse hit timestamp (stat rows carry naive ISO strings)
        try:
            hit_dt = _parse_iso_utc(hit.timestamp)
        except (ValueError, AttributeError):
            result.append(hit)
            continue

        suppressed = False
        for fault_ch, onset_dt, end_dt in windows:
            # Exact column match — only the specific faulted channel
            # is suppressed, not other columns in the same sensor slot.
            if hit.column != fault_ch:
                continue
            if hit_dt < onset_dt:
                continue
            if end_dt is None or hit_dt <= end_dt:
                suppressed = True
                break

        if not suppressed:
            result.append(hit)

    return result



def _row_hit_detail(hit: RowHit) -> UploadHighAnomalyDetail:
    """A core ``RowHit`` (OOB or look-ahead spike) as the upload schema."""
    return UploadHighAnomalyDetail(
        score=1.0,
        timestamp=hit.timestamp,
        detector=hit.detector,
        column=hit.column,
        severity=hit.severity,
        method=hit.method,
        value=hit.value,
    )


def _rolling_hit_detail(hit: RollingAnomaly) -> StatisticalAnomalyDetail:
    """A core rolling-z hit as the upload schema."""
    return StatisticalAnomalyDetail(
        timestamp=hit.timestamp,
        column=hit.column,
        value=hit.value,
        z_score=float(hit.z_score),
        severity=hit.severity,
        method=hit.method,
    )


def score_portion(
    *,
    normalized_df: pd.DataFrame | None,
    prev_overlap_df: pd.DataFrame,
    raw_rows: list[dict[str, Any]],
    state: UploadStreamState,
    adaptive_ranges: list[tuple[float, float]],
    detector: AnomalyDetector,
    seq_len: int = 20,
    include_embeddings: bool = False,
) -> tuple[PortionScores, pd.DataFrame, UploadStreamState]:
    """Score one portion (stream stages + LSTM).

    Returns ``(result, next_overlap_df, state_after)``. ``state`` is not
    modified. The OOB / duration-stuck / rolling-z / look-ahead spike
    detectors are the core stages in ``score_upload_stream`` (ADR-014 A7);
    this function maps their outputs to the upload schema and runs the LSTM.

    `prev_overlap_df` must hold the last seq_len-1 normalized rows of the
    previous portion so sequences spanning the boundary are not lost.
    ``state.stuck`` is ``None`` when duration-stuck is not run; baseline
    exclusion is then skipped too.
    """
    result = PortionScores(rows_processed=len(raw_rows))

    output, state_after = score_upload_stream(
        normalized_df, raw_rows, state, _UPLOAD_CONTEXT, BATCH_PROFILE
    )
    result.oob_hits = [_row_hit_detail(h) for h in output.oob_hits]
    result.stuck_hits = [_stuck_dict_to_high_detail(a.as_dict()) for a in output.stuck_alerts]
    result.stat_hits = [_rolling_hit_detail(h) for h in output.rolling_z_hits]
    result.mad_hits = [_row_hit_detail(h) for h in output.spike_hits]

    # The clamped new rows every later stage scored (the input when no OOB hit).
    normalized_df = output.scoring_frame
    if normalized_df is None or normalized_df.empty:
        return result, prev_overlap_df, state_after

    # Clamp the overlap tail too — in the async path the producer serialises
    # unclamped rows, so prev_overlap_df may carry OOB values from an earlier
    # portion.  In the sync path the tail was already clamped at the previous
    # call, so this is a no-op for in-range values.
    if not prev_overlap_df.empty:
        prev_overlap_clamped = clamp_frame(prev_overlap_df)
        combined_df = pd.concat([prev_overlap_clamped, normalized_df], ignore_index=True)
    else:
        combined_df = normalized_df

    # Next-overlap uses the clamped normalized_df so the tail never carries
    # raw OOB values into the following portion.
    if len(normalized_df) >= seq_len - 1:
        next_overlap = normalized_df.iloc[-(seq_len - 1):].copy()
    elif len(combined_df) >= seq_len - 1:
        next_overlap = combined_df.iloc[-(seq_len - 1):].copy()
    else:
        next_overlap = combined_df.copy()

    if len(combined_df) < seq_len:
        return result, next_overlap, state_after

    raw_data = combined_df[SENSOR_COLS].to_numpy(dtype=np.float32)
    norm_data = normalize_data(raw_data, adaptive_ranges)
    sequences = preprocess_sequences(norm_data, seq_len=seq_len)
    raw_scores, chunk_embeddings, chunk_uncertainties = detector.predict_batch_enriched(
        sequences
    )
    batch_scores = np.clip(raw_scores / ANOMALY_SCALING_FACTOR, 0.0, 1.0)

    result.scores = batch_scores.tolist()
    result.sequence_timestamps = _sequence_timestamps_for(
        combined_df, len(batch_scores), seq_len
    )
    if include_embeddings:
        result.embeddings = np.asarray(chunk_embeddings).tolist()
        result.uncertainties = np.asarray(chunk_uncertainties).tolist()
    return result, next_overlap, state_after


def finalize_scores(
    portions_in_order: list[PortionScores],
    *,
    tenant_id: str,
    greenhouse_id: str,
    imputed_channels: list[str],
    state: UploadStreamState,
    include_embeddings: bool = False,
) -> tuple[UploadInferenceResponse, list[str], UploadStreamState]:
    """Calibrate the file-level threshold over all portions and build the response.

    calibrate_threshold needs the whole score distribution, so this is the one
    step that must see every portion — the inputs are compact (floats, ISO
    strings, sparse hits), not raw rows.

    ``state`` is the stream state after the last portion (the async path
    rebuilds it from the stored portion results). When ``state.stuck`` is not
    ``None``, still-open freezes are flushed and the cadence check is
    finalised.  All stuck_fault alerts (from portions + flush) are merged into
    ``high_anomalies``. The third return value is ``state`` after the flush,
    for callers that persist the final stuck carry.
    """
    # Local import: cold_run_upload also imports this module.
    from src.api.services.cold_run_upload import _merge_by_timestamp

    anomaly_scores: list[float] = []
    sequence_timestamps: list[str] = []
    embeddings: list[list[float]] = []
    uncertainties: list[float] = []
    stat_details: list[StatisticalAnomalyDetail] = []
    stuck_details: list[UploadHighAnomalyDetail] = []
    mad_details: list[UploadHighAnomalyDetail] = []
    oob_details: list[UploadHighAnomalyDetail] = []
    for portion in portions_in_order:
        anomaly_scores.extend(portion.scores)
        sequence_timestamps.extend(portion.sequence_timestamps)
        stat_details.extend(portion.stat_hits)
        stuck_details.extend(portion.stuck_hits)
        mad_details.extend(portion.mad_hits)
        oob_details.extend(portion.oob_hits)
        if include_embeddings:
            embeddings.extend(portion.embeddings)
            uncertainties.extend(portion.uncertainties)

    # Fault-window suppression: suppress statistical hits on faulted
    # channels inside [onset, release + N].  Applied BEFORE pair
    # suppression so that a suppressed humidity hit does not prevent
    # a paired temperature hit from being cancelled.
    stat_details = _suppress_fault_window_hits(
        stat_details, state.stuck.to_carry() if state.stuck is not None else None
    )

    # Psychrometric suppression: suppress paired air-temperature +
    # humidity statistical alerts when measured HD confirms the pairing
    # is expected physics (cooling air → rising RH).  Inconsistent HD
    # is flagged.  Restricted to the canonical "temperature" slot
    # (air temperature only; pipe/heating temps are excluded).
    stat_details = _suppress_psychrometric_pairs(
        stat_details, state.rolling_z.psychro_context,
    )

    # Flush open freezes and finalise cadence check.
    if state.stuck is not None:
        flush_alerts, flushed = flush_duration_stuck(state.stuck, BATCH_PROFILE)
        stuck_details.extend(_stuck_dict_to_high_detail(a.as_dict()) for a in flush_alerts)
        state = dataclasses.replace(state, stuck=flushed)

    scores_arr = np.asarray(anomaly_scores, dtype=np.float64)
    threshold_used, threshold_method = _calibrate_upload_threshold(scores_arr)

    lstm_high = [
        UploadHighAnomalyDetail(
            sequence_index=seq_idx,
            # The LSTM's own decision point is the file-calibrated threshold, so
            # that is its `p`.  Canonicalising here rather than at persist time is
            # what makes this list single-scale: _merge_by_timestamp takes
            # max(lstm, statistical) and episode grouping derives peak_score from
            # it, and neither is meaningful while the two detectors disagree about
            # what the number means.
            score=_lstm_score_to_canonical(float(score), threshold_used),
            score_raw=float(score),
            timestamp=sequence_timestamps[seq_idx]
            if seq_idx < len(sequence_timestamps)
            else "unknown",
            detector="lstm",
        )
        for seq_idx, score in enumerate(anomaly_scores)
        if score > threshold_used
    ]

    statistical_summary = StatisticalColdRunSummary(
        anomalies=stat_details,
        baseline_columns=list(state.rolling_z.columns or ()),
        total_scored=max(
            0,
            state.rolling_z.total_rows
            - _effective_rolling_window(state.rolling_z.total_rows, COLD_RUN_STAT_ROLLING_WINDOW),
        ),
    )
    stat_high = [
        UploadHighAnomalyDetail(
            score=_stat_z_to_canonical(detail.z_score),
            score_raw=detail.z_score,
            timestamp=detail.timestamp,
            detector="statistical",
            column=detail.column,
            z_score=detail.z_score,
            severity=detail.severity,
            method=detail.method,
            value=detail.value,
        )
        for detail in stat_details
        if detail.severity in ("critical", "warning")
    ]

    high_anomalies = _merge_by_timestamp(lstm_high, stat_high)

    # Append stuck-fault, MAD-spike, and OOB alerts (distinct detectors — no
    # timestamp merge with the LSTM/statistical pair).
    if stuck_details:
        high_anomalies.extend(stuck_details)
    if mad_details:
        high_anomalies.extend(mad_details)
    if oob_details:
        high_anomalies.extend(oob_details)
    if stuck_details or mad_details or oob_details:
        high_anomalies.sort(key=lambda h: h.timestamp)

    # Task 5: group contiguous per-channel alerts into episodes.
    # recovery_channels (removed from StuckCarry in BACKLOG-5, not merely
    # omitted from this call) is not threaded through here -- cross-channel
    # recovery tagging is query-time only (docs/handoff/task5_spec.md),
    # since stuck_carry is per-upload and cannot see a fault released in an
    # earlier upload.
    final_carry = state.stuck.to_carry() if state.stuck is not None else None
    done_faults = final_carry.completed_faults if final_carry else []
    open_freezes = final_carry.channels if final_carry else {}
    raw_episodes = group_alerts_into_episodes(
        high_anomalies,
        completed_faults=done_faults,
        open_freezes=open_freezes,
    )
    episodes = [
        AnomalyEpisodeSchema(**ep.to_dict()) for ep in raw_episodes
    ]

    response = UploadInferenceResponse(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        sequences_processed=len(anomaly_scores),
        anomalies_detected=len(high_anomalies),
        anomaly_scores=anomaly_scores,
        high_anomalies=high_anomalies,
        episodes=episodes,
        threshold_used=float(threshold_used),
        threshold_method=threshold_method,
        imputed_channels=imputed_channels,
        statistical_summary=statistical_summary,
        embeddings=embeddings,
        uncertainties=uncertainties,
        embedding_dim=LATENT_DIM,
        sequence_timestamps=sequence_timestamps,
    )
    return response, sequence_timestamps, state

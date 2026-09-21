"""Per-portion CSV scoring core.

Shared by the synchronous upload path (run_cold_run_upload drives it chunk by
chunk) and the RabbitMQ upload worker (one portion per message). Memory is
bounded per portion: only anomaly scores, sequence timestamps and sparse
statistical hits survive a portion; full raw rows and embeddings do not
(embeddings are retained only when a caller opts in).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
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
    COLD_RUN_STAT_MIN_CONSECUTIVE,
    COLD_RUN_STAT_ROLLING_WINDOW,
    COLD_RUN_STAT_Z_MIN,
    LATENT_DIM,
)
from src.models.psychrometric_validator import validate_row as _psychro_validate_row
from src.models.cold_run_detector import (
    ColdRunStatisticalDetector,
    _effective_rolling_window,
)
from src.models.cold_run_threshold import calibrate_threshold
from src.models.data_utils import normalize_data, preprocess_sequences
from src.models.inference import AnomalyDetector
from src.models.episode_grouper import group_alerts_into_episodes
from src.models.stuck_fault_detector import (
    StuckCarry,
    WallclockStuckDetector,
)


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
    rows_processed: int = 0


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
        z_score=alert["duration_hours"],
        value=alert["value"],
    )


# Singleton: reused across score_portion calls (stateless — state is in StuckCarry).
_WALLCLOCK_DETECTOR = WallclockStuckDetector()


def _detect_stats_incremental(
    raw_rows: list[dict[str, Any]],
    stat_carry: StatCarry,
) -> list[StatisticalAnomalyDetail]:
    """Rolling statistical detection over carry+portion, deduped across portions.

    The carry keeps the last ``COLD_RUN_STAT_ROLLING_WINDOW +
    COLD_RUN_STAT_MIN_CONSECUTIVE - 1`` raw rows so that:

    * the rolling baseline has enough history (``window`` rows), and
    * a streak of ``min_consecutive`` candidates straddling a chunk
      boundary is not severed (the extra ``min_consecutive - 1`` rows
      preserve the tail of a streak started in the prior portion).

    In incremental mode the configured ``COLD_RUN_STAT_ROLLING_WINDOW``
    is always used as-is (no short-file scaling) and positions are
    masked by **global** row index (rows with global index < window
    can never fire), matching the reference single-chunk computation.

    Hits already reported for a ``(timestamp, column)`` pair are dropped
    via ``stat_carry.seen`` as a safety-net dedup.
    """
    if not raw_rows:
        return []
    detector = ColdRunStatisticalDetector()
    if stat_carry.columns is None:
        stat_carry.columns = detector._discover_columns(raw_rows)

    carry_rows = stat_carry.rows
    rows = carry_rows + raw_rows

    # Global row index of carry_rows[0] (or raw_rows[0] when no carry).
    n_prior = max(0, stat_carry.total_rows - len(carry_rows))
    scored_prefix = len(carry_rows)

    hits = detector.detect_rolling(
        rows,
        columns=stat_carry.columns,
        n_prior_rows=n_prior,
        scored_prefix=scored_prefix,
    )

    fresh: list[StatisticalAnomalyDetail] = []
    for hit in hits:
        key = (hit.timestamp, hit.column)
        if key in stat_carry.seen:
            continue
        stat_carry.seen.add(key)
        fresh.append(
            StatisticalAnomalyDetail(
                timestamp=hit.timestamp,
                column=hit.column,
                value=hit.value,
                z_score=float(hit.z_score),
                severity=hit.severity,
                method=hit.method,
            )
        )

    # Extract psychrometric context (T_air, RH, measured_hd) for timestamps
    # with fresh anomalies, so finalize_scores can suppress physically-expected
    # paired temperature + humidity alerts.
    hit_timestamps = {h.timestamp for h in fresh}
    if hit_timestamps:
        _extract_psychro_context(rows, hit_timestamps, stat_carry)

    # Carry length: window (for rolling baseline) + min_consecutive - 1
    # (for streak continuity across chunk boundaries).
    carry_len = COLD_RUN_STAT_ROLLING_WINDOW + COLD_RUN_STAT_MIN_CONSECUTIVE - 1
    stat_carry.rows = rows[-carry_len:]
    stat_carry.total_rows += len(raw_rows)
    return fresh


def _extract_psychro_context(
    rows: list[dict[str, Any]],
    timestamps: set[str],
    stat_carry: StatCarry,
) -> None:
    """Store (T_air, RH, measured_hd) for anomaly timestamps when all three present.

    Only records context for timestamps not already stored, so the carry
    accumulates across portions without duplicating work.
    """
    for row in rows:
        ts = row.get("timestamp", "")
        if ts not in timestamps or ts in stat_carry.psychro_context:
            continue
        t_val = row.get("temperature")
        rh_val = row.get("humidity")
        hd_val = row.get("measured_hd")
        if t_val is None or rh_val is None or hd_val is None:
            continue
        try:
            t_f = float(t_val)
            rh_f = float(rh_val)
            hd_f = float(hd_val)
        except (TypeError, ValueError):
            continue
        if math.isfinite(t_f) and math.isfinite(rh_f) and math.isfinite(hd_f):
            stat_carry.psychro_context[ts] = (t_f, rh_f, hd_f)


def _is_temp_stat_column(col: str) -> bool:
    """True for temperature-family columns in statistical alerts."""
    return "temp" in col.lower()


def _is_humidity_stat_column(col: str) -> bool:
    """True for relative-humidity columns (not humidity deficit / measured_hd)."""
    return "humid" in col.lower()


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
        has_temp = any(_is_temp_stat_column(d.column) for d in details)
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
            and (_is_temp_stat_column(d.column) or _is_humidity_stat_column(d.column))
        )
    ]
    result.extend(inconsistent_extras)
    return result


def _purge_fault_from_stat_carry(
    stat_carry: StatCarry,
    channel: str,
    onset_ts: str,
    end_ts: str,
) -> None:
    """NaN-out a channel's values in stat_carry.rows for the fault period.

    This prevents the rolling baseline from being contaminated by the
    frozen values.  After purge, the rolling detector builds its
    baseline from pre-fault data and the z-scores after recovery are
    computed against a clean baseline (Task 3 — address the baseline,
    not only the emission).
    """
    for row in stat_carry.rows:
        row_ts = row.get("timestamp", "")
        if onset_ts <= row_ts <= end_ts and channel in row:
            row[channel] = float("nan")


def score_portion(
    *,
    normalized_df: pd.DataFrame | None,
    prev_overlap_df: pd.DataFrame,
    raw_rows: list[dict[str, Any]],
    stat_carry: StatCarry,
    adaptive_ranges: list[tuple[float, float]],
    detector: AnomalyDetector,
    seq_len: int = 20,
    include_embeddings: bool = False,
    stuck_carry: StuckCarry | None = None,
) -> tuple[PortionScores, pd.DataFrame]:
    """Score one portion (LSTM + statistical + stuck). Returns (result, next_overlap_df).

    `prev_overlap_df` must hold the last seq_len-1 normalized rows of the
    previous portion so sequences spanning the boundary are not lost.

    When ``stuck_carry`` is provided, wall-clock stuck-fault detection runs
    FIRST so recovery channels can be identified before statistical detection.
    The carry threads per-channel freeze state across portions.
    """
    result = PortionScores(rows_processed=len(raw_rows))

    # Run stuck detection FIRST so we know which channels just
    # exited a fault before the statistical detector runs.
    if stuck_carry is not None and normalized_df is not None and not normalized_df.empty:
        raw_alerts = _WALLCLOCK_DETECTOR.detect_chunk(normalized_df, stuck_carry)
        result.stuck_hits = [_stuck_dict_to_high_detail(a) for a in raw_alerts]

        # Task 3 baseline cleanup: purge fault-period values from
        # stat_carry so the rolling z-score baseline is not contaminated
        # by frozen readings.
        for ch, fw in stuck_carry.recovery_channels.items():
            _purge_fault_from_stat_carry(
                stat_carry, ch, fw.onset_ts, fw.end_ts,
            )

    result.stat_hits = _detect_stats_incremental(raw_rows, stat_carry)

    if normalized_df is None or normalized_df.empty:
        return result, prev_overlap_df

    if not prev_overlap_df.empty:
        combined_df = pd.concat([prev_overlap_df, normalized_df], ignore_index=True)
    else:
        combined_df = normalized_df

    if len(normalized_df) >= seq_len - 1:
        next_overlap = normalized_df.iloc[-(seq_len - 1):].copy()
    elif len(combined_df) >= seq_len - 1:
        next_overlap = combined_df.iloc[-(seq_len - 1):].copy()
    else:
        next_overlap = combined_df.copy()

    if len(combined_df) < seq_len:
        return result, next_overlap

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
    return result, next_overlap


def finalize_scores(
    portions_in_order: list[PortionScores],
    *,
    tenant_id: str,
    greenhouse_id: str,
    imputed_channels: list[str],
    stat_carry: StatCarry,
    include_embeddings: bool = False,
    stuck_carry: StuckCarry | None = None,
) -> tuple[UploadInferenceResponse, list[str]]:
    """Calibrate the file-level threshold over all portions and build the response.

    calibrate_threshold needs the whole score distribution, so this is the one
    step that must see every portion — the inputs are compact (floats, ISO
    strings, sparse hits), not raw rows.

    When ``stuck_carry`` is provided, still-open freezes are flushed and the
    cadence check is finalised.  All stuck_fault alerts (from portions + flush)
    are merged into ``high_anomalies``.
    """
    # Local import: cold_run_upload also imports this module.
    from src.api.services.cold_run_upload import _merge_by_timestamp

    anomaly_scores: list[float] = []
    sequence_timestamps: list[str] = []
    embeddings: list[list[float]] = []
    uncertainties: list[float] = []
    stat_details: list[StatisticalAnomalyDetail] = []
    stuck_details: list[UploadHighAnomalyDetail] = []
    for portion in portions_in_order:
        anomaly_scores.extend(portion.scores)
        sequence_timestamps.extend(portion.sequence_timestamps)
        stat_details.extend(portion.stat_hits)
        stuck_details.extend(portion.stuck_hits)
        if include_embeddings:
            embeddings.extend(portion.embeddings)
            uncertainties.extend(portion.uncertainties)

    # Psychrometric suppression: suppress paired temperature + humidity
    # statistical alerts when measured HD confirms the pairing is expected
    # physics (cooling air → rising RH).  Inconsistent HD is flagged.
    stat_details = _suppress_psychrometric_pairs(
        stat_details, stat_carry.psychro_context,
    )

    # Flush open freezes and finalise cadence check.
    if stuck_carry is not None:
        flush_alerts = _WALLCLOCK_DETECTOR.flush(stuck_carry)
        stuck_details.extend(_stuck_dict_to_high_detail(a) for a in flush_alerts)

    scores_arr = np.asarray(anomaly_scores, dtype=np.float64)
    threshold_used, threshold_method = calibrate_threshold(scores_arr)

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
        baseline_columns=stat_carry.columns or [],
        total_scored=max(
            0,
            stat_carry.total_rows
            - _effective_rolling_window(stat_carry.total_rows, COLD_RUN_STAT_ROLLING_WINDOW),
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

    # Append stuck-fault alerts (distinct detector — no timestamp merge needed).
    if stuck_details:
        high_anomalies.extend(stuck_details)
        high_anomalies.sort(key=lambda h: h.timestamp)

    # Task 5: group contiguous per-channel alerts into episodes.
    recovery_chs = stuck_carry.recovery_channels if stuck_carry else {}
    done_faults = stuck_carry.completed_faults if stuck_carry else []
    raw_episodes = group_alerts_into_episodes(
        high_anomalies,
        recovery_channels=recovery_chs,
        completed_faults=done_faults,
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
    return response, sequence_timestamps

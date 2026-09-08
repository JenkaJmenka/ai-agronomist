"""Per-portion CSV scoring core.

Shared by the synchronous upload path (run_cold_run_upload drives it chunk by
chunk) and the RabbitMQ upload worker (one portion per message). Memory is
bounded per portion: only anomaly scores, sequence timestamps and sparse
statistical hits survive a portion; full raw rows and embeddings do not
(embeddings are retained only when a caller opts in).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.api.schemas.sensor_data import (
    StatisticalAnomalyDetail,
    StatisticalColdRunSummary,
    UploadHighAnomalyDetail,
    UploadInferenceResponse,
)
from src.api.services.score_transform import canonical as _canonical_transform
from src.config import (
    ANOMALY_SCALING_FACTOR,
    COLD_RUN_STAT_ROLLING_WINDOW,
    COLD_RUN_STAT_Z_MIN,
    LATENT_DIM,
)
from src.models.cold_run_detector import (
    ColdRunStatisticalDetector,
    _effective_rolling_window,
)
from src.models.cold_run_threshold import calibrate_threshold
from src.models.data_utils import normalize_data, preprocess_sequences
from src.models.inference import AnomalyDetector


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

SENSOR_COLS = ["temperature", "humidity", "co2", "light"]


@dataclass
class StatCarry:
    """Rolling state threaded between portions for statistical detection."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    seen: set[tuple[str, str]] = field(default_factory=set)
    total_rows: int = 0
    columns: list[str] | None = None


@dataclass
class PortionScores:
    """Compact per-portion inference output (safe to serialize for MQ jobs)."""

    scores: list[float] = field(default_factory=list)
    sequence_timestamps: list[str] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    uncertainties: list[float] = field(default_factory=list)
    stat_hits: list[StatisticalAnomalyDetail] = field(default_factory=list)
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


def _detect_stats_incremental(
    raw_rows: list[dict[str, Any]],
    stat_carry: StatCarry,
) -> list[StatisticalAnomalyDetail]:
    """Rolling statistical detection over carry+portion, deduped across portions.

    The carry keeps the last COLD_RUN_STAT_ROLLING_WINDOW raw rows so portion
    boundaries do not blind the rolling baseline; hits already reported for a
    (timestamp, column) pair are dropped.
    """
    if not raw_rows:
        return []
    detector = ColdRunStatisticalDetector()
    if stat_carry.columns is None:
        stat_carry.columns = detector._discover_columns(raw_rows)
    rows = stat_carry.rows + raw_rows
    hits = detector.detect_rolling(rows, columns=stat_carry.columns)

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

    stat_carry.rows = rows[-COLD_RUN_STAT_ROLLING_WINDOW:]
    stat_carry.total_rows += len(raw_rows)
    return fresh


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
) -> tuple[PortionScores, pd.DataFrame]:
    """Score one portion (LSTM + statistical). Returns (result, next_overlap_df).

    `prev_overlap_df` must hold the last seq_len-1 normalized rows of the
    previous portion so sequences spanning the boundary are not lost.
    """
    result = PortionScores(rows_processed=len(raw_rows))
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
) -> tuple[UploadInferenceResponse, list[str]]:
    """Calibrate the file-level threshold over all portions and build the response.

    calibrate_threshold needs the whole score distribution, so this is the one
    step that must see every portion — the inputs are compact (floats, ISO
    strings, sparse hits), not raw rows.
    """
    # Local import: cold_run_upload also imports this module.
    from src.api.services.cold_run_upload import _merge_by_timestamp

    anomaly_scores: list[float] = []
    sequence_timestamps: list[str] = []
    embeddings: list[list[float]] = []
    uncertainties: list[float] = []
    stat_details: list[StatisticalAnomalyDetail] = []
    for portion in portions_in_order:
        anomaly_scores.extend(portion.scores)
        sequence_timestamps.extend(portion.sequence_timestamps)
        stat_details.extend(portion.stat_hits)
        if include_embeddings:
            embeddings.extend(portion.embeddings)
            uncertainties.extend(portion.uncertainties)

    scores_arr = np.asarray(anomaly_scores, dtype=np.float64)
    threshold_used, threshold_method = calibrate_threshold(scores_arr)

    lstm_high = [
        UploadHighAnomalyDetail(
            sequence_index=seq_idx,
            score=float(score),
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

    response = UploadInferenceResponse(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        sequences_processed=len(anomaly_scores),
        anomalies_detected=len(high_anomalies),
        anomaly_scores=anomaly_scores,
        high_anomalies=high_anomalies,
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

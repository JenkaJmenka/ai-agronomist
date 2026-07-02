"""Cold-run CSV upload: LSTM scoring + statistical detection."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from src.api.schemas.sensor_data import (
    StatisticalAnomalyDetail,
    StatisticalColdRunSummary,
    UploadHighAnomalyDetail,
    UploadInferenceResponse,
)
from src.config import ANOMALY_SCALING_FACTOR, COLD_RUN_STAT_ROLLING_WINDOW, LATENT_DIM
from src.models.cold_run_detector import (
    ColdRunStatisticalDetector,
    _effective_rolling_window,
)
from src.models.cold_run_threshold import calibrate_threshold
from src.models.data_utils import (
    SENSOR_RANGES,
    compute_adaptive_sensor_ranges,
    normalize_data,
    preprocess_sequences,
)
from src.models.inference import AnomalyDetector
from src.vendors.csv_adapter import CSVAdapter, detect_optional_sensor_presence, parse_raw_rows
from src.api.services.csv_persist import normalize_timestamp_key


def build_anomaly_scores_by_timestamp(
    high_anomalies: list[UploadHighAnomalyDetail],
) -> dict[str, float]:
    """Map flagged cold-run hits to ISO timestamps for persistence.

    Only anomalies above the file-calibrated threshold are stored. Normal
    sequence scores stay unset so dashboard filtering stays sparse and accurate.
    """
    scores: dict[str, float] = {}
    for hit in high_anomalies:
        key = normalize_timestamp_key(hit.timestamp)
        if key is None:
            continue
        scores[key] = max(scores.get(key, 0.0), float(hit.score))
    return scores


def build_anomaly_metadata_by_timestamp(
    high_anomalies: list[UploadHighAnomalyDetail],
) -> dict[str, dict[str, list[str]]]:
    """Per-timestamp anomaly columns and detectors for dashboard attribution."""
    metadata: dict[str, dict[str, list[str]]] = {}
    for hit in high_anomalies:
        key = normalize_timestamp_key(hit.timestamp)
        if key is None:
            continue
        entry = metadata.setdefault(key, {"columns": [], "detectors": []})
        detector = hit.detector or "lstm"
        if detector not in entry["detectors"]:
            entry["detectors"].append(detector)
        if hit.column and hit.column not in entry["columns"]:
            entry["columns"].append(hit.column)
    return metadata


def _read_chunks(tmp_path: str, chunk_size: int, csv_props: dict[str, Any]):
    return pd.read_csv(
        tmp_path,
        chunksize=chunk_size,
        sep=csv_props.get("sep", ","),
        encoding=csv_props.get("encoding", "utf-8"),
        decimal=csv_props.get("decimal", "."),
    )


def run_cold_run_upload(
    *,
    tmp_path: str,
    adapter: CSVAdapter,
    detector: AnomalyDetector,
    tenant_id: str,
    greenhouse_id: str,
    seq_len: int = 20,
    chunk_size: int = 5000,
) -> tuple[UploadInferenceResponse, list[str]]:
    """Score an uploaded CSV with file-calibrated LSTM + statistical detectors."""
    csv_props = adapter._csv_props
    sensor_cols = ["temperature", "humidity", "co2", "light"]

    header_cols: list[str] | None = None
    imputed_channels: list[str] = []
    adaptive_ranges = list(SENSOR_RANGES)
    stats_parts: list[pd.DataFrame] = []
    raw_readings: list[dict[str, Any]] = []

    for raw_chunk in _read_chunks(tmp_path, chunk_size, csv_props):
        if raw_chunk.empty:
            continue
        if header_cols is None:
            header_cols = list(raw_chunk.columns)
            _, imputed = detect_optional_sensor_presence(header_cols)
            imputed_channels = sorted(imputed)
        try:
            normalized_chunk = adapter.normalize(raw_chunk)
            adapter.validate(normalized_chunk)
            stats_parts.append(normalized_chunk[sensor_cols])
        except Exception:
            pass

        raw_df, _ = parse_raw_rows(raw_chunk)
        if raw_df.empty:
            continue

        # Vectorize timestamp conversion
        raw_df["timestamp"] = raw_df["timestamp"].map(
            lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x)
        )

        # Fast record-oriented NaN stripping
        for row in raw_df.to_dict(orient="records"):
            entry = {
                "timestamp": row["timestamp"],
                **{
                    col: val for col, val in row.items()
                    if col not in ("timestamp", "source")
                    and val is not None and not (isinstance(val, float) and pd.isna(val))
                }
            }
            raw_readings.append(entry)

    if not stats_parts:
        raise ValueError("Uploaded CSV contains no valid data.")

    stats_df = pd.concat(stats_parts, ignore_index=True)
    adaptive_ranges = compute_adaptive_sensor_ranges(
        stats_df.to_numpy(dtype=np.float32),
        SENSOR_RANGES,
    )

    anomaly_scores: list[float] = []
    embeddings: list[list[float]] = []
    uncertainties: list[float] = []
    sequence_timestamps: list[str] = []
    sequences_processed = 0
    prev_overlap_df = pd.DataFrame()

    for raw_chunk in _read_chunks(tmp_path, chunk_size, csv_props):
        if raw_chunk.empty:
            continue
        try:
            normalized_chunk = adapter.normalize(raw_chunk)
            adapter.validate(normalized_chunk)
        except Exception:
            continue

        if not prev_overlap_df.empty:
            combined_df = pd.concat([prev_overlap_df, normalized_chunk], ignore_index=True)
        else:
            combined_df = normalized_chunk

        if len(normalized_chunk) >= seq_len - 1:
            prev_overlap_df = normalized_chunk.iloc[-(seq_len - 1) :].copy()
        elif len(combined_df) >= seq_len - 1:
            prev_overlap_df = combined_df.iloc[-(seq_len - 1) :].copy()
        else:
            prev_overlap_df = combined_df.copy()

        if len(combined_df) < seq_len:
            continue

        raw_data = combined_df[sensor_cols].to_numpy(dtype=np.float32)
        norm_data = normalize_data(raw_data, adaptive_ranges)
        sequences = preprocess_sequences(norm_data, seq_len=seq_len)
        raw_scores, chunk_embeddings, chunk_uncertainties = detector.predict_batch_enriched(
            sequences
        )
        batch_scores = np.clip(raw_scores / ANOMALY_SCALING_FACTOR, 0.0, 1.0)
        sequences_processed += len(sequences)
        anomaly_scores.extend(batch_scores.tolist())
        embeddings.extend(np.asarray(chunk_embeddings).tolist())
        uncertainties.extend(np.asarray(chunk_uncertainties).tolist())

        timestamps = combined_df["timestamp"].tolist()
        for idx in range(len(batch_scores)):
            row_idx = idx + seq_len - 1
            if row_idx < len(timestamps):
                ts_val = timestamps[row_idx]
                if pd.isnull(ts_val):
                    sequence_timestamps.append("unknown")
                else:
                    sequence_timestamps.append(
                        ts_val.isoformat()
                        if hasattr(ts_val, "isoformat")
                        else str(ts_val)
                    )
            else:
                sequence_timestamps.append("unknown")

    scores_arr = np.asarray(anomaly_scores, dtype=np.float64)
    threshold_used, threshold_method = calibrate_threshold(scores_arr)

    lstm_high: list[UploadHighAnomalyDetail] = []
    for seq_idx, score in enumerate(anomaly_scores):
        if score > threshold_used:
            lstm_high.append(
                UploadHighAnomalyDetail(
                    sequence_index=seq_idx,
                    score=float(score),
                    timestamp=sequence_timestamps[seq_idx]
                    if seq_idx < len(sequence_timestamps)
                    else "unknown",
                    detector="lstm",
                )
            )

    statistical_summary = _run_statistical_detection(raw_readings)
    stat_high = [
        UploadHighAnomalyDetail(
            score=min(1.0, detail.z_score / 5.0),
            timestamp=detail.timestamp,
            detector="statistical",
            column=detail.column,
            z_score=detail.z_score,
            severity=detail.severity,
            method=detail.method,
            value=detail.value,
        )
        for detail in statistical_summary.anomalies
        if detail.severity in ("critical", "warning")
    ]

    high_anomalies = _merge_by_timestamp(lstm_high, stat_high)
    anomalies_detected = len(high_anomalies)

    return (
        UploadInferenceResponse(
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            sequences_processed=sequences_processed,
            anomalies_detected=anomalies_detected,
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
        ),
        sequence_timestamps,
    )


def _merge_timestamp_key(timestamp: str) -> str:
    """Canonical key so LSTM and statistical hits align on the same instant."""
    normalized = normalize_timestamp_key(timestamp)
    return normalized if normalized is not None else timestamp


def _merge_by_timestamp(
    lstm_hits: list[UploadHighAnomalyDetail],
    stat_hits: list[UploadHighAnomalyDetail],
) -> list[UploadHighAnomalyDetail]:
    """Merge LSTM and statistical hits into one row per timestamp.

    When both detectors flag the same timestamp:
    - score = max(lstm_score, stat_score) for highest severity
    - detector = "lstm+statistical"
    - agreement = True
    - statistical fields (column, z_score, severity, method, value) are carried in

    Statistical-only hits and LSTM-only hits are kept as-is with agreement=False.
    Result is sorted by timestamp ascending.
    """
    lstm_by_ts: dict[str, UploadHighAnomalyDetail] = {}
    for hit in lstm_hits:
        key = _merge_timestamp_key(hit.timestamp)
        existing = lstm_by_ts.get(key)
        if existing is None or hit.score > existing.score:
            lstm_by_ts[key] = hit

    merged: list[UploadHighAnomalyDetail] = []
    stat_matched: set[str] = set()

    for stat in stat_hits:
        key = _merge_timestamp_key(stat.timestamp)
        if key in lstm_by_ts:
            lstm = lstm_by_ts[key]
            merged.append(
                UploadHighAnomalyDetail(
                    score=max(lstm.score, stat.score),
                    timestamp=stat.timestamp,
                    detector="lstm+statistical",
                    sequence_index=lstm.sequence_index,
                    column=stat.column,
                    z_score=stat.z_score,
                    severity=stat.severity,
                    method=stat.method,
                    value=stat.value,
                    agreement=True,
                )
            )
            stat_matched.add(key)
        else:
            merged.append(stat)

    for key, hit in lstm_by_ts.items():
        if key not in stat_matched:
            merged.append(hit)

    merged.sort(key=lambda h: h.timestamp)
    return merged


def _run_statistical_detection(
    raw_readings: list[dict[str, Any]],
) -> StatisticalColdRunSummary:
    if not raw_readings:
        return StatisticalColdRunSummary()

    stat_detector = ColdRunStatisticalDetector()
    rolling_hits = stat_detector.detect_rolling(raw_readings)
    anomalies: list[StatisticalAnomalyDetail] = [
        StatisticalAnomalyDetail(
            timestamp=hit.timestamp,
            column=hit.column,
            value=hit.value,
            z_score=float(hit.z_score),
            severity=hit.severity,
            method=hit.method,
        )
        for hit in rolling_hits
    ]

    return StatisticalColdRunSummary(
        anomalies=anomalies,
        baseline_columns=stat_detector._discover_columns(raw_readings),
        total_scored=max(
            0,
            len(raw_readings)
            - _effective_rolling_window(len(raw_readings), COLD_RUN_STAT_ROLLING_WINDOW),
        ),
    )

"""Cold-run CSV upload: LSTM scoring + statistical detection."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.sensor_data import (
    UploadHighAnomalyDetail,
    UploadInferenceResponse,
)
from src.api.services.score_transform import canonical as _canonical_score
from src.config import ADAPTIVE_RANGE_SAMPLE_ROWS
from src.models.data_utils import SENSOR_RANGES, AdaptiveRangeAccumulator
from src.models.inference import AnomalyDetector
from src.vendors.csv_adapter import CSVAdapter, detect_optional_sensor_presence, parse_raw_rows
from src.api.services.csv_persist import (
    apply_anomaly_scores,
    normalize_timestamp_key,
    persist_readings_chunk,
)
from src.api.services.upload_portions import (
    SENSOR_COLS,
    PortionScores,
    StatCarry,
    StuckCarry,
    finalize_scores,
    score_portion,
)


class EmptyCsvError(ValueError):
    """Raised when the uploaded CSV contains no data rows at all (HTTP 400)."""


def build_anomaly_scores_by_timestamp(
    high_anomalies: list[UploadHighAnomalyDetail],
) -> dict[str, float]:
    """Map flagged cold-run hits to ISO timestamps for persistence.

    Only anomalies above the file-calibrated threshold are stored. Normal
    sequence scores stay unset so dashboard filtering stays sparse and accurate.

    .. deprecated::
        Prefer ``build_upload_score_dicts`` which also computes canonical scores
        and raw scores for ADR-005 provenance columns.
    """
    scores: dict[str, float] = {}
    for hit in high_anomalies:
        key = normalize_timestamp_key(hit.timestamp)
        if key is None:
            continue
        scores[key] = max(scores.get(key, 0.0), float(hit.score))
    return scores


def build_upload_score_dicts(
    high_anomalies: list[UploadHighAnomalyDetail],
) -> tuple[dict[str, float], dict[str, float | None]]:
    """Build (canonical_by_ts, raw_by_ts) dicts for the upload write path.

    ``hit.score`` is already canonical — each producer canonicalises against its
    own decision point in ``finalize_scores`` (the LSTM against the per-file
    ``threshold_used``, the statistical detector against ``COLD_RUN_STAT_Z_MIN``).
    Transforming again here applied the LSTM's threshold to scores that were
    already canonical: a second, wrong mapping that inflated statistical hits to
    ~0.82-0.89 while LSTM hits sat at ~0.50-0.53, and that would have pushed
    every hit below the 0.5 display threshold had ``threshold_used`` been high.

    The highest canonical score per timestamp wins, and that hit's native score
    travels with it for the provenance column.

    Returns
    -------
    canonical_by_ts:
        ADR-005 canonical scores for ``SensorReading.anomaly_score``.
    raw_by_ts:
        Producer-native scores for ``SensorReading.anomaly_score_raw``.  Carries a
        key for every key in ``canonical_by_ts``, with None where the producer has
        no native scalar (stuck_fault), so the two never disagree on keys.
    """
    best_by_ts: dict[str, UploadHighAnomalyDetail] = {}
    for hit in high_anomalies:
        key = normalize_timestamp_key(hit.timestamp)
        if key is None or not math.isfinite(hit.score):
            continue
        current = best_by_ts.get(key)
        if current is None or hit.score > current.score:
            best_by_ts[key] = hit

    canonical_by_ts: dict[str, float] = {}
    raw_by_ts: dict[str, float | None] = {}
    for key, hit in best_by_ts.items():
        canonical_by_ts[key] = float(hit.score)
        raw = hit.score_raw
        raw_by_ts[key] = float(raw) if raw is not None and math.isfinite(raw) else None

    return canonical_by_ts, raw_by_ts


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


def build_trigger_data_by_timestamp(
    high_anomalies: list[UploadHighAnomalyDetail],
) -> dict[str, dict[str, object]]:
    """Per-timestamp primary trigger: channel with highest |z|, its detector/severity/value.

    For rows where multiple columns fire at the same timestamp the highest-|z|
    column wins.  LSTM-only hits (column=None, z_score=None) are included so
    trigger_detector is always populated; trigger_channel stays None for them.
    """
    # {ts_key: {"channel", "detector", "severity", "value", "_z"}}
    best: dict[str, dict[str, object]] = {}
    for hit in high_anomalies:
        key = normalize_timestamp_key(hit.timestamp)
        if key is None:
            continue
        z = abs(hit.z_score) if hit.z_score is not None else 0.0
        existing = best.get(key)
        if existing is None or z > abs(existing.get("_z") or 0.0):
            best[key] = {
                "channel": hit.column,
                "detector": hit.detector or "lstm",
                "severity": hit.severity,
                "value": hit.value,
                "_z": hit.z_score,
            }
    # Strip the internal sort key before returning.
    return {
        k: {
            "channel": v["channel"],
            "detector": v["detector"],
            "severity": v["severity"],
            "value": v["value"],
        }
        for k, v in best.items()
    }


def _read_chunks(tmp_path: str, chunk_size: int, csv_props: dict[str, Any]):
    from src.vendors.csv_adapter import pandas_csv_reader_kwargs

    return pd.read_csv(
        tmp_path,
        chunksize=chunk_size,
        **pandas_csv_reader_kwargs(csv_props),
    )


def _strip_raw_records(raw_df: pd.DataFrame) -> list[dict[str, Any]]:
    """Record-oriented NaN stripping with ISO timestamps (statistical detector input)."""
    ts_iso = raw_df["timestamp"].map(
        lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x)
    )
    dropped_df = raw_df.drop(columns=["timestamp", "source"], errors="ignore")
    records: list[dict[str, Any]] = []
    for ts, row in zip(ts_iso, dropped_df.to_dict(orient="records")):
        records.append(
            {
                "timestamp": ts,
                **{
                    col: val
                    for col, val in row.items()
                    if val is not None
                    and not (isinstance(val, float) and pd.isna(val))
                },
            }
        )
    return records


def _calibrate_ranges_from_head(
    tmp_path: str,
    chunk_size: int,
    csv_props: dict[str, Any],
    adapter: CSVAdapter,
) -> tuple[list[tuple[float, float]], list[str]]:
    """Head-sample pass: adaptive ranges + imputed-channel detection.

    Reads until ADAPTIVE_RANGE_SAMPLE_ROWS valid rows are seen (whole file for
    smaller uploads), so scoring never has to hold the full file in memory.
    """
    header_cols: list[str] | None = None
    imputed_channels: list[str] = []
    range_acc = AdaptiveRangeAccumulator(len(SENSOR_COLS))
    valid_rows = 0
    total_rows = 0

    try:
        chunks = _read_chunks(tmp_path, chunk_size, csv_props)
        for raw_chunk in chunks:
            if raw_chunk.empty:
                continue
            total_rows += len(raw_chunk)
            if header_cols is None:
                header_cols = list(raw_chunk.columns)
                _, imputed = detect_optional_sensor_presence(header_cols)
                imputed_channels = sorted(imputed)
            try:
                normalized_chunk = adapter.normalize(raw_chunk)
                adapter.validate(normalized_chunk)
                range_acc.update(normalized_chunk[SENSOR_COLS].to_numpy(dtype=np.float32))
                valid_rows += len(normalized_chunk)
            except Exception:
                pass
            if valid_rows >= ADAPTIVE_RANGE_SAMPLE_ROWS:
                break
    except pd.errors.EmptyDataError as exc:
        raise EmptyCsvError("Uploaded CSV contains no valid data.") from exc

    if valid_rows == 0:
        if total_rows == 0:
            raise EmptyCsvError("Uploaded CSV contains no valid data.")
        raise ValueError("Uploaded CSV contains no valid data.")

    return range_acc.ranges(SENSOR_RANGES), imputed_channels


async def run_cold_run_upload(
    *,
    tmp_path: str,
    adapter: CSVAdapter,
    detector: AnomalyDetector,
    tenant_id: str,
    greenhouse_id: str,
    seq_len: int = 20,
    chunk_size: int = 5000,
    include_embeddings: bool = False,
    persist_db: AsyncSession | None = None,
) -> tuple[UploadInferenceResponse, list[str]]:
    """Score an uploaded CSV with file-calibrated LSTM + statistical detectors.

    Single scoring pass with bounded memory: adaptive ranges come from a head
    sample, statistical detection streams with a rolling carry, and rows are
    persisted chunk-by-chunk when `persist_db` is provided (scores for
    above-threshold hits are attached in a sparse post-pass once the
    file-level threshold is calibrated).
    """
    csv_props = adapter._csv_props

    adaptive_ranges, imputed_channels = _calibrate_ranges_from_head(
        tmp_path, chunk_size, csv_props, adapter
    )

    portions: list[PortionScores] = []
    stat_carry = StatCarry()
    stuck_carry = StuckCarry(skip_channels=frozenset(imputed_channels))
    prev_overlap_df = pd.DataFrame()
    sensor_rows_persisted = 0
    raw_rows_persisted = 0

    for raw_chunk in _read_chunks(tmp_path, chunk_size, csv_props):
        if raw_chunk.empty:
            continue

        normalized_chunk: pd.DataFrame | None
        try:
            normalized_chunk = adapter.normalize(raw_chunk)
            adapter.validate(normalized_chunk)
        except Exception:
            normalized_chunk = None

        raw_df, _ = parse_raw_rows(raw_chunk)
        raw_rows = _strip_raw_records(raw_df) if not raw_df.empty else []

        portion, prev_overlap_df = score_portion(
            normalized_df=normalized_chunk,
            prev_overlap_df=prev_overlap_df,
            raw_rows=raw_rows,
            stat_carry=stat_carry,
            adaptive_ranges=adaptive_ranges,
            detector=detector,
            seq_len=seq_len,
            include_embeddings=include_embeddings,
            stuck_carry=stuck_carry,
        )
        portions.append(portion)

        if persist_db is not None:
            s_ins, s_upd, r_ins, r_upd = await persist_readings_chunk(
                db=persist_db,
                tenant_id=tenant_id,
                greenhouse_id=greenhouse_id,
                df=normalized_chunk,
                raw_df=raw_df if not raw_df.empty else None,
            )
            await persist_db.commit()
            sensor_rows_persisted += s_ins + s_upd
            raw_rows_persisted += r_ins + r_upd

    response, sequence_timestamps = finalize_scores(
        portions,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        imputed_channels=imputed_channels,
        stat_carry=stat_carry,
        include_embeddings=include_embeddings,
        stuck_carry=stuck_carry,
    )

    if persist_db is not None:
        if sensor_rows_persisted + raw_rows_persisted == 0:
            raise HTTPException(
                status_code=422,
                detail="No data could be extracted from the uploaded CSV. "
                "Check column names and data format.",
            )
        # ADR-005: canonical + native score dicts keyed by timestamp. The
        # per-file P99.9 calibration (response.threshold_used) is applied where
        # the LSTM hits are built, in finalize_scores — not here, or statistical
        # hits would be canonicalised a second time against the wrong p.
        _canonical_by_ts, _raw_by_ts = build_upload_score_dicts(
            response.high_anomalies,
        )
        await apply_anomaly_scores(
            db=persist_db,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            anomaly_scores_by_timestamp=_canonical_by_ts,
            anomaly_metadata_by_timestamp=build_anomaly_metadata_by_timestamp(
                response.high_anomalies
            ),
            anomaly_score_raw_by_timestamp=_raw_by_ts,
            score_scale="upload",
            trigger_data_by_timestamp=build_trigger_data_by_timestamp(
                response.high_anomalies
            ),
        )
        response = response.model_copy(
            update={
                "data_persisted": True,
                "rows_uploaded": sensor_rows_persisted or raw_rows_persisted,
                "sensor_rows_uploaded": sensor_rows_persisted,
                "raw_rows_uploaded": raw_rows_persisted,
            }
        )

    return response, sequence_timestamps


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
            # Both sides are canonical (ADR-005), so max() compares like with
            # like and score_raw follows whichever score won — mixing a winning
            # score with the loser's native value would misreport provenance.
            winner = lstm if lstm.score >= stat.score else stat
            merged.append(
                UploadHighAnomalyDetail(
                    score=winner.score,
                    score_raw=winner.score_raw,
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



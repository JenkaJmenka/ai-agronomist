"""Anomaly score backfill — score already-persisted SensorReading rows.

Re-runnable: re-scoring the same window with the same model produces the same
result (stable calibration over a fixed dataset).  Only non-live rows are touched.
Non-finite scores are stored as SQL NULL (ADR-005 §2.4; Postgres safety).

Algorithm (two passes over the database):

1. Collect — page through rows in bounded batches (cursor pagination), build
   SEQ_LEN sliding windows with overlap across page boundaries, run LSTM-VAE
   inference, accumulate (timestamp, raw_score) pairs in memory.

2. Calibrate — derive file-level threshold from all collected scores using the
   same ``calibrate_threshold`` the upload path uses; compute canonical [0,1]
   score for hits above threshold via ``score_transform.canonical``.

3. Write — sparse UPDATE: only above-threshold rows receive an anomaly_score.
   Normal rows (below threshold) remain NULL — matching upload-path semantics.

Intentionally avoids the upload path's statistical detector: the statistical
z-score computation needs raw CSV rows with all original columns, and only the
normalised SensorReading values are persisted.  The LSTM score backfill is the
correct and complete signal available from persisted data.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from sqlalchemy import bindparam, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.score_transform import canonical as _canonical
from src.config import ANOMALY_SCALING_FACTOR, SEQ_LEN
from src.db.models import SensorReading
from src.models.cold_run_threshold import calibrate_threshold
from src.models.data_utils import SENSOR_RANGES, normalize_data, preprocess_sequences
from src.models.inference import AnomalyDetector

logger = logging.getLogger(__name__)

# Provenance label consistent with the CSV upload write path.
_SCORE_SCALE = "upload"

# Max parameter rows per executemany UPDATE to avoid oversized queries.
_UPDATE_BATCH = 2000

# Columns pulled from DB to form LSTM sequences (must match SENSOR_COLS order).
_SENSOR_COLS = ("temperature", "humidity", "co2", "light")


@dataclass
class BackfillResult:
    """Summary returned by :func:`backfill_scores`."""

    tenant_id: str
    greenhouse_id: str
    rows_examined: int
    sequences_scored: int
    rows_scored: int
    threshold_used: float
    threshold_method: str

    # Informational counters.
    live_rows_skipped: int = 0
    batches_processed: int = 0

    # Accumulated warnings — e.g. non-finite raw scores that were stored as NULL.
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _apply_range_padding(
    vmin: float,
    vmax: float,
    default: tuple[float, float],
) -> tuple[float, float]:
    """Replicate AdaptiveRangeAccumulator padding logic (5% of span)."""
    if not (math.isfinite(vmin) and math.isfinite(vmax)):
        return default
    if vmax - vmin < 1e-6:
        return default
    pad = max((vmax - vmin) * 0.05, 1e-3)
    return (vmin - pad, vmax + pad)


async def _compute_adaptive_ranges(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
) -> list[tuple[float, float]]:
    """Compute adaptive normalization ranges from the window via one SQL query.

    Falls back to ``SENSOR_RANGES`` per-column when a column has no valid data
    or is flat (zero variance), exactly as AdaptiveRangeAccumulator.ranges() does.
    """
    stmt = select(
        func.min(SensorReading.temperature),
        func.max(SensorReading.temperature),
        func.min(SensorReading.humidity),
        func.max(SensorReading.humidity),
        func.min(SensorReading.co2),
        func.max(SensorReading.co2),
        func.min(SensorReading.light),
        func.max(SensorReading.light),
    ).where(
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
        SensorReading.timestamp >= start,
        SensorReading.timestamp <= end,
        or_(
            SensorReading.source.is_(None),
            SensorReading.source != "live",
        ),
    )
    result = await db.execute(stmt)
    row = result.one()
    # row = (t_min, t_max, h_min, h_max, c_min, c_max, l_min, l_max)
    ranges: list[tuple[float, float]] = []
    for col_idx, default in enumerate(SENSOR_RANGES):
        agg_min = row[col_idx * 2]
        agg_max = row[col_idx * 2 + 1]
        if agg_min is None or agg_max is None:
            ranges.append(default)
        else:
            ranges.append(_apply_range_padding(float(agg_min), float(agg_max), default))
    return ranges


async def _fetch_batch(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    after_ts: Optional[datetime],
    limit: int,
) -> list[SensorReading]:
    """Cursor-page SensorReading rows, excluding source='live'."""
    conds = [
        SensorReading.tenant_id == tenant_id,
        SensorReading.greenhouse_id == greenhouse_id,
        SensorReading.timestamp <= end,
        or_(
            SensorReading.source.is_(None),
            SensorReading.source != "live",
        ),
    ]
    if after_ts is None:
        conds.append(SensorReading.timestamp >= start)
    else:
        conds.append(SensorReading.timestamp > after_ts)

    stmt = (
        select(SensorReading)
        .where(*conds)
        .order_by(SensorReading.timestamp)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _rows_to_array(rows: list[SensorReading]) -> np.ndarray:
    """Extract sensor columns into a float32 array of shape (T, 4)."""
    return np.array(
        [[r.temperature, r.humidity, r.co2, r.light] for r in rows],
        dtype=np.float32,
    )


def _score_working_set(
    working_rows: list[SensorReading],
    overlap_len: int,
    ranges: list[tuple[float, float]],
    detector: AnomalyDetector,
) -> list[tuple[datetime, float]]:
    """Run LSTM-VAE inference on the working set; return (ts, raw_score) pairs.

    Only pairs whose terminal row is a NEW row (index >= overlap_len) are
    returned — overlap rows were already scored in the previous batch.

    Raw score = np.clip(mse / ANOMALY_SCALING_FACTOR, 0, 1), identical to the
    upload path (``score_portion`` in upload_portions.py:392).
    """
    if len(working_rows) < SEQ_LEN:
        return []

    raw_data = _rows_to_array(working_rows)
    norm_data = normalize_data(raw_data, ranges)
    sequences = preprocess_sequences(norm_data, seq_len=SEQ_LEN)
    # sequences shape: (N, SEQ_LEN, 4) where N = len(working_rows) - SEQ_LEN + 1

    raw_mse, _, _ = detector.predict_batch_enriched(sequences)
    batch_scores = np.clip(raw_mse / ANOMALY_SCALING_FACTOR, 0.0, 1.0)

    pairs: list[tuple[datetime, float]] = []
    for seq_idx, score in enumerate(batch_scores):
        # The terminal row of sequence seq_idx is working_rows[seq_idx + SEQ_LEN - 1].
        terminal_idx = seq_idx + SEQ_LEN - 1
        if terminal_idx < overlap_len:
            # Terminal row is from the overlap window — already scored.
            continue
        ts = working_rows[terminal_idx].timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        pairs.append((ts, float(score)))

    return pairs


async def _write_scores(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    scored: list[tuple[datetime, float, Optional[float]]],
) -> None:
    """Batch UPDATE anomaly_score, anomaly_score_raw, score_scale for scored rows.

    Each entry in ``scored`` is (timestamp, canonical_score_or_None, raw_score).
    canonical_score=None is written as SQL NULL (ADR-005 §2.4 guard).

    Writes in sub-batches of _UPDATE_BATCH to avoid oversized executemany calls.
    """
    if not scored:
        return

    table = SensorReading.__table__
    stmt = (
        update(table)
        .where(
            table.c.tenant_id == tenant_id,
            table.c.greenhouse_id == greenhouse_id,
            table.c.timestamp == bindparam("ts"),
        )
        .values(
            anomaly_score=bindparam("score"),
            anomaly_score_raw=bindparam("raw_score"),
            score_scale=bindparam("scale"),
        )
    )

    for batch_start in range(0, len(scored), _UPDATE_BATCH):
        batch = scored[batch_start : batch_start + _UPDATE_BATCH]
        params = [
            {
                "ts": ts,
                "score": canonical_score,
                "raw_score": raw_score,
                "scale": _SCORE_SCALE,
            }
            for ts, canonical_score, raw_score in batch
        ]
        await db.execute(stmt, params)

    await db.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def backfill_scores(
    *,
    db: AsyncSession,
    detector: AnomalyDetector,
    tenant_id: str,
    greenhouse_id: str,
    start: datetime,
    end: datetime,
    batch_size: int = 5000,
) -> BackfillResult:
    """Score already-persisted SensorReading rows for a (tenant, greenhouse, window).

    Parameters
    ----------
    db:
        Async database session — the caller owns commit/rollback lifecycle.
    detector:
        Loaded AnomalyDetector instance.  Use ``get_cached_detector(None)``
        to get the current baseline model.
    tenant_id, greenhouse_id:
        Scope the backfill to one greenhouse.  Rows from OTHER tenants/
        greenhouses are never touched (tenant isolation).
    start, end:
        Inclusive timestamp window.  NOT clamped to wall-clock now() — the
        historical CSV is future-dated (2026) relative to the browser clock.
    batch_size:
        Max SensorReading rows fetched per DB round-trip.  Controls peak
        memory.  The actual number of sequences per batch is
        batch_size + SEQ_LEN - 1 (overlap).

    Returns
    -------
    BackfillResult
        Summary with rows_examined, rows_scored, threshold_used, and warnings.
    """
    result = BackfillResult(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        rows_examined=0,
        sequences_scored=0,
        rows_scored=0,
        threshold_used=0.0,
        threshold_method="percentile",
    )

    # Ensure timezone-awareness (treat naïve datetimes as UTC).
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    # Count live rows for the informational field (cheap aggregate query).
    live_count_row = await db.execute(
        select(func.count()).where(
            SensorReading.tenant_id == tenant_id,
            SensorReading.greenhouse_id == greenhouse_id,
            SensorReading.timestamp >= start,
            SensorReading.timestamp <= end,
            SensorReading.source == "live",
        )
    )
    result.live_rows_skipped = live_count_row.scalar_one() or 0

    # --- Compute adaptive normalization ranges (one aggregate query) ----------
    ranges = await _compute_adaptive_ranges(db, tenant_id, greenhouse_id, start, end)

    # --- Pass 1: page through rows, collect (timestamp, raw_score) pairs ------
    all_pairs: list[tuple[datetime, float]] = []  # (ts, raw_scaled_score)
    overlap_rows: list[SensorReading] = []
    after_ts: Optional[datetime] = None
    total_rows = 0

    while True:
        new_rows = await _fetch_batch(
            db, tenant_id, greenhouse_id, start, end, after_ts, batch_size
        )
        if not new_rows:
            break

        total_rows += len(new_rows)
        working = overlap_rows + new_rows

        pairs = _score_working_set(
            working,
            overlap_len=len(overlap_rows),
            ranges=ranges,
            detector=detector,
        )
        all_pairs.extend(pairs)

        # Keep last SEQ_LEN-1 rows of the WORKING set as overlap for the next
        # page.  Using the full working set (not just new_rows) ensures that
        # sequences spanning a page boundary are included: e.g. when batch_size
        # is small relative to SEQ_LEN, the overlap contains rows from several
        # previous pages.
        overlap_rows = working[-(SEQ_LEN - 1):]
        after_ts = new_rows[-1].timestamp
        result.batches_processed += 1

        if len(new_rows) < batch_size:
            break  # Final partial batch.

    result.rows_examined = total_rows
    result.sequences_scored = len(all_pairs)

    if not all_pairs:
        logger.info(
            "backfill_scores: no sequences scored for %s/%s [%s, %s]",
            tenant_id, greenhouse_id, start.isoformat(), end.isoformat(),
        )
        return result

    # --- Pass 2: calibrate threshold -----------------------------------------
    raw_scores_arr = np.asarray([s for _, s in all_pairs], dtype=np.float64)
    threshold_used, threshold_method = calibrate_threshold(raw_scores_arr)
    result.threshold_used = float(threshold_used)
    result.threshold_method = threshold_method

    # --- Pass 3: compute canonical scores and write back ---------------------
    scored: list[tuple[datetime, float, Optional[float]]] = []
    for ts, raw_score in all_pairs:
        if raw_score <= threshold_used:
            continue  # Below threshold — leave anomaly_score as NULL.

        canonical_score = _canonical(
            raw_score,
            threshold_used,
            None,  # unbounded branch, matching upload path
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            timestamp=ts.isoformat(),
        )

        if canonical_score is None:
            # Non-finite input — store as NULL (ADR-005 §2.4).
            warn_msg = (
                f"non-finite canonical for raw={raw_score!r} at {ts.isoformat()} "
                f"(threshold={threshold_used!r}); stored as SQL NULL"
            )
            logger.warning("backfill_scores: %s", warn_msg)
            result.warnings.append(warn_msg)
            # Still write the row so score_scale is set for provenance.
            scored.append((ts, None, raw_score if math.isfinite(raw_score) else None))
        else:
            scored.append((ts, canonical_score, raw_score))

    await _write_scores(db, tenant_id, greenhouse_id, scored)
    result.rows_scored = len(scored)

    logger.info(
        "backfill_scores: tenant=%s gh=%s window=[%s,%s] "
        "examined=%d sequences=%d scored=%d threshold=%.4f(%s) batches=%d",
        tenant_id, greenhouse_id,
        start.isoformat(), end.isoformat(),
        result.rows_examined,
        result.sequences_scored,
        result.rows_scored,
        result.threshold_used,
        result.threshold_method,
        result.batches_processed,
    )
    return result

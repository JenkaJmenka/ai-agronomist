"""Server-side downsampling for readings endpoints (migration plan §5 G1).

LTTB (Largest-Triangle-Three-Buckets) over the timestamp axis, with one
hard guarantee: rows carrying significant anomaly scores survive
downsampling even when LTTB would drop them — anomaly peaks are the whole
point of the chart.

Pure functions only; no framework imports (unit-testable in isolation).
"""

from __future__ import annotations

from typing import Any

from src.config import DOWNSAMPLE_ANOMALY_BUDGET_RATIO, DOWNSAMPLE_ANOMALY_QUANTILE

_TIMESTAMP_KEY = "timestamp"
_SCORE_KEY = "anomaly_score"


def _numeric_channels(rows: list[dict[str, Any]]) -> list[str]:
    """Channel keys with numeric values, sampled from the head of the series."""
    channels: list[str] = []
    for row in rows[:50]:
        for key, value in row.items():
            if key in (_TIMESTAMP_KEY, _SCORE_KEY) or key in channels:
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                channels.append(key)
    return channels


def _representative_value(row: dict[str, Any], channels: list[str]) -> float:
    """Mean of available numeric channels; the LTTB y-axis."""
    values = [
        float(row[ch])
        for ch in channels
        if isinstance(row.get(ch), (int, float)) and not isinstance(row.get(ch), bool)
    ]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _lttb_indices(ys: list[float], n_out: int) -> list[int]:
    """Classic LTTB bucket selection over evenly indexed samples.

    Uses the row index as the x-axis (rows are timestamp-ordered), which is
    equivalent to LTTB on time for near-uniform cadence and avoids parsing
    timestamps in the hot path.
    """
    n = len(ys)
    if n_out >= n or n_out < 3:
        return list(range(n))

    indices = [0]
    bucket_size = (n - 2) / (n_out - 2)

    prev_idx = 0
    for bucket in range(n_out - 2):
        bucket_start = int(bucket * bucket_size) + 1
        bucket_end = min(int((bucket + 1) * bucket_size) + 1, n - 1)
        next_start = bucket_end
        next_end = min(int((bucket + 2) * bucket_size) + 1, n)

        # Average point of the NEXT bucket forms the triangle's third vertex.
        next_span = max(next_end - next_start, 1)
        avg_x = (next_start + next_end - 1) / 2
        avg_y = sum(ys[next_start:next_end]) / next_span if next_end > next_start else ys[-1]

        best_idx = bucket_start
        best_area = -1.0
        for i in range(bucket_start, bucket_end):
            area = abs(
                (prev_idx - avg_x) * (ys[i] - ys[prev_idx])
                - (prev_idx - i) * (avg_y - ys[prev_idx])
            )
            if area > best_area:
                best_area = area
                best_idx = i
        indices.append(best_idx)
        prev_idx = best_idx

    indices.append(n - 1)
    return indices


def _anomaly_indices(
    rows: list[dict[str, Any]],
    max_points: int,
    threshold: float | None = None,
) -> set[int]:
    """Indices to preserve through downsampling.

    With ``threshold`` (the client's display threshold), every row the client
    would highlight is preserved — that is the set the chart paints bands
    over, so dropping any of them makes a real anomaly silently unbandable.

    The threshold path is skipped when the flagged rows cannot fit within
    ``DOWNSAMPLE_ANOMALY_BUDGET_RATIO`` of the budget: the live path scores
    every reading, so a low threshold can flag the whole window and there
    would be nothing left to downsample.

    Without ``threshold`` (or when it flags too much) a row is preserved when
    its score clears BOTH the configured top quantile and the midpoint
    between the median and the max — so uniform low scores mark nothing,
    while sparse spikes are always kept. That heuristic protects only the top
    decile, which is why callers that know their threshold should pass it.
    """
    scored = [
        (i, float(row[_SCORE_KEY]))
        for i, row in enumerate(rows)
        if isinstance(row.get(_SCORE_KEY), (int, float))
    ]
    if not scored:
        return set()

    if threshold is not None:
        flagged = {i for i, score in scored if score >= threshold}
        if flagged and len(flagged) <= int(max_points * DOWNSAMPLE_ANOMALY_BUDGET_RATIO):
            return flagged

    values = sorted(score for _, score in scored)
    median = values[len(values) // 2]
    peak = values[-1]
    if peak <= median or peak <= 0:
        return set()  # no distinguishable anomalies in this window
    quantile_cut = values[min(int(len(values) * DOWNSAMPLE_ANOMALY_QUANTILE), len(values) - 1)]
    cutoff = max(quantile_cut, median + 0.5 * (peak - median))
    return {i for i, score in scored if score >= cutoff}


def downsample_rows(
    rows: list[dict[str, Any]],
    max_points: int,
    threshold: float | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Reduce `rows` to at most `max_points`, preserving anomaly rows.

    Pass ``threshold`` (the client's display threshold) so the rows the
    client will highlight survive; see :func:`_anomaly_indices`.

    Returns (rows, downsampled). Order is preserved (input must be
    timestamp-ordered, as the readings endpoints guarantee).
    """
    if max_points <= 0 or len(rows) <= max_points:
        return rows, False

    channels = _numeric_channels(rows)
    ys = [_representative_value(row, channels) for row in rows]
    keep = set(_lttb_indices(ys, max_points))
    anomalies = _anomaly_indices(rows, max_points, threshold)
    keep |= anomalies

    if len(keep) > max_points:
        # Thin only non-anomaly rows: evenly spaced picks until we fit.
        non_anomaly = sorted(keep - anomalies)
        budget = max(max_points - len(anomalies), 2)
        if budget >= len(non_anomaly):
            thinned = set(non_anomaly)
        else:
            stride = len(non_anomaly) / budget
            last = len(non_anomaly) - 1
            thinned = {non_anomaly[min(int(i * stride), last)] for i in range(budget)}
        # Endpoints always survive so the series never loses its extent.
        thinned |= {0, len(rows) - 1}
        keep = thinned | anomalies

    return [rows[i] for i in sorted(keep)], True


def strip_scores(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the anomaly_score key (include_scores=False payload trim)."""
    return [{k: v for k, v in row.items() if k != _SCORE_KEY} for row in rows]

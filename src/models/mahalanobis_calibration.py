"""History calibration for the multivariate Mahalanobis detector.

The self-fit path keeps a fixed chi-square cutoff. The history path can
take its cutoff from the history's own distances instead, after the caller
narrows those rows to a clock-hour band. These helpers stay pure: they do
not read a database and they do not invent timestamps. The default
quantile is ``MAHALANOBIS_DISTANCE_QUANTILE``. On the July clear-sky
fixture that default still flags more than 2% of clean evaluation
windows, so shipping it does not re-arm the pipeline vote. The measured
counts are recorded beside the constant.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

import numpy as np

from src.config import (
    MAHALANOBIS_DISTANCE_QUANTILE,
    MAHALANOBIS_HOUR_WINDOW,
)

# Covariance needs two finite rows per sensor. Below that the caller keeps
# the chi-square fallback instead of an empirical cutoff.
_MIN_ROWS_PER_SENSOR = 2


def empirical_mahalanobis_cutoff(
    history: np.ndarray,
    floors: np.ndarray,
    quantile: float = MAHALANOBIS_DISTANCE_QUANTILE,
) -> Optional[float]:
    """High quantile of squared Mahalanobis distances of ``history``.

    The fit is the same median center and diagonal noise floor as
    ``MultivariateMahalanobisDetector._distances``: distances are the
    history rows scored under a covariance fit on those same rows.

    Args:
        history: Physical rows, shape ``(n, n_sensors)``.
        floors: Per-sensor noise floors, one value per column.
        quantile: Tail level in ``(0, 1)``.

    Returns:
        The quantile cutoff, or ``None`` when fewer than two finite rows
        per sensor are available or the covariance cannot be inverted.
        ``None`` tells the caller to use ``MAHALANOBIS_CHI2_THRESHOLD``.
    """
    rows = _as_history_matrix(history)
    level = _require_quantile(quantile)
    finite = rows[np.isfinite(rows).all(axis=1)]
    n_sensors = int(rows.shape[1])
    if int(finite.shape[0]) < _MIN_ROWS_PER_SENSOR * n_sensors:
        return None
    floor_vector = _as_floors(floors, n_sensors)
    distances = _squared_distances(finite, finite, floor_vector)
    if distances is None or not np.isfinite(distances).all():
        return None
    cutoff = float(np.quantile(distances, level, method="linear"))
    if not np.isfinite(cutoff) or cutoff <= 0.0:
        return None
    return cutoff


def select_hour_conditioned_rows(
    history: np.ndarray,
    timestamps: Sequence[datetime],
    reference: datetime,
    hour_window: int = MAHALANOBIS_HOUR_WINDOW,
) -> np.ndarray:
    """Rows whose clock hour lies within ``hour_window`` of ``reference``.

    The hour is the timestamp's own wall clock. Naive and aware values are
    not converted, and missing timestamps are not filled in. Distance wraps
    at midnight, so hour 0 with window 1 keeps hours 23, 0, and 1.

    Args:
        history: Physical rows, shape ``(n, n_sensors)``, aligned with
            ``timestamps``.
        timestamps: One datetime per history row.
        reference: Clock hour the scored sample belongs to.
        hour_window: Non-negative half-width in hours.

    Returns:
        The matching finite rows. An empty array with the same sensor
        count means the band has fewer than ``2 * n_sensors`` finite rows,
        so the caller should fall back. Excluding the scored calendar day from
    ``history`` is the caller's job; this function only applies the hour band.
    """
    rows = _as_history_matrix(history)
    times = _aligned_datetimes(timestamps, int(rows.shape[0]))
    window = _require_hour_window(hour_window)
    reference_hour = _require_datetime(reference).hour
    n_sensors = int(rows.shape[1])
    keep = _hour_band_indices(rows, times, reference_hour, window)
    if len(keep) < _MIN_ROWS_PER_SENSOR * n_sensors:
        return np.empty((0, n_sensors), dtype=float)
    return rows[np.asarray(keep, dtype=int)].copy()


def _squared_distances(
    seq: np.ndarray,
    fit_rows: np.ndarray,
    floors: np.ndarray,
) -> Optional[np.ndarray]:
    """Median-centered squared Mahalanobis distances, diagonal noise floor."""
    center = np.median(fit_rows, axis=0)
    cov = np.cov(fit_rows.T) + np.diag(floors**2)
    try:
        inv_cov = np.linalg.inv(cov)
    except np.linalg.LinAlgError:
        return None
    centered = seq - center
    return np.einsum("ij,jk,ik->i", centered, inv_cov, centered)


def _hour_band_indices(
    rows: np.ndarray,
    times: Sequence[datetime],
    reference_hour: int,
    hour_window: int,
) -> list[int]:
    keep: list[int] = []
    for index, stamp in enumerate(times):
        if _hour_delta(stamp.hour, reference_hour) > hour_window:
            continue
        if not np.isfinite(rows[index]).all():
            continue
        keep.append(index)
    return keep


def _hour_delta(left: int, right: int) -> int:
    delta = abs(int(left) - int(right)) % 24
    return min(delta, 24 - delta)


def _as_history_matrix(history: np.ndarray) -> np.ndarray:
    rows = np.asarray(history, dtype=float)
    if rows.ndim != 2:
        raise ValueError(
            f"Expected 2-D history (n, sensors), got {rows.ndim}-D"
        )
    return rows


def _as_floors(floors: np.ndarray, n_sensors: int) -> np.ndarray:
    vector = np.asarray(floors, dtype=float)
    if vector.shape != (n_sensors,):
        raise ValueError(
            f"floors must have shape ({n_sensors},), got {vector.shape}"
        )
    if not np.isfinite(vector).all() or np.any(vector < 0.0):
        raise ValueError("floors must be finite and >= 0")
    return vector


def _aligned_datetimes(
    timestamps: Sequence[datetime],
    n_rows: int,
) -> list[datetime]:
    times = [_require_datetime(value) for value in timestamps]
    if len(times) != n_rows:
        raise ValueError(
            "timestamps and history differ in length: "
            f"{len(times)} vs {n_rows}"
        )
    return times


def _require_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    raise TypeError(
        "timestamps must be datetime instances, "
        f"got {type(value).__name__}"
    )


def _require_hour_window(hour_window: int) -> int:
    if isinstance(hour_window, bool) or not isinstance(
        hour_window, (int, np.integer)
    ):
        raise TypeError(
            f"hour_window must be an int, got {type(hour_window).__name__}"
        )
    window = int(hour_window)
    if window < 0:
        raise ValueError(f"hour_window must be >= 0, got {window}")
    return window


def _require_quantile(quantile: float) -> float:
    level = float(quantile)
    if not np.isfinite(level) or not 0.0 < level < 1.0:
        raise ValueError(f"quantile must be in (0, 1), got {quantile}")
    return level

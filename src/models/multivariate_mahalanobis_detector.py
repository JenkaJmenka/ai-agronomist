"""Per-timestep multivariate Mahalanobis joint-implausibility check (plan P3).

Per-channel detectors miss readings that are individually in range but
jointly impossible (hot air + saturated humidity + low CO2 at noon). This
detector fits a robust center/covariance over the sequence window and
flags timesteps whose Mahalanobis distance exceeds a chi-square cutoff.

Robustness choices:
- Trimmed refit: distances are computed with a median-centered covariance,
  the most extreme ~10% of timesteps are dropped, and center/covariance are
  refit on the rest — so an outlier cannot mask itself by inflating the fit.
  (MinCovDet was evaluated and rejected: at seq_len=20 its distances are far
  from chi-square calibrated, flagging ordinary Gaussian points.)
- The covariance diagonal is floored at the per-sensor noise variance so
  near-constant windows cannot alarm on measurement noise (the same
  pathology the spike-MAD floor guards against).
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np

from src.config import MAHALANOBIS_CHI2_THRESHOLD, SENSOR_NAMES, SENSOR_NOISE_TARGETS

# Fraction of the window trimmed before the final covariance refit.
_TRIM_FRACTION = 0.1


class MultivariateMahalanobisDetector:
    """Flag jointly implausible timesteps in a (seq_len, n_sensors) window."""

    def __init__(
        self,
        chi2_threshold: float = MAHALANOBIS_CHI2_THRESHOLD,
        noise_floors: Optional[dict[str, float]] = None,
    ) -> None:
        if chi2_threshold <= 0:
            raise ValueError(f"chi2_threshold must be > 0, got {chi2_threshold}")
        self._threshold = chi2_threshold
        self._noise_floors = dict(noise_floors or SENSOR_NOISE_TARGETS)

    def _floor_vector(self, n_sensors: int) -> np.ndarray:
        floors = [
            self._noise_floors.get(name, 1.0)
            for name in SENSOR_NAMES[:n_sensors]
        ]
        return np.asarray(floors, dtype=float)

    def detect(self, sequence: np.ndarray) -> List[bool]:
        """Flag each timestep whose joint reading is implausible.

        Args:
            sequence: 2-D array ``(seq_len, n_sensors)`` in physical units.

        Returns:
            List of booleans, one per timestep.
        """
        seq = np.asarray(sequence, dtype=float)
        if seq.ndim != 2:
            raise ValueError(f"Expected 2-D (seq_len, sensors), got {seq.ndim}-D")
        n, dims = seq.shape
        # Need clearly more samples than dimensions for a usable covariance.
        if n < dims * 2 or not np.isfinite(seq).all():
            return [False] * n

        floors = self._floor_vector(dims)

        # Pass 1: distances under a median-centered fit on the full window.
        d2 = self._distances(seq, seq, floors)
        if d2 is None:
            return [False] * n

        # Pass 2: refit without the most extreme timesteps so an outlier
        # cannot inflate the covariance it is judged against.
        n_trim = max(1, int(round(n * _TRIM_FRACTION)))
        keep = np.argsort(d2)[: n - n_trim]
        d2 = self._distances(seq, seq[keep], floors)
        if d2 is None:
            return [False] * n

        return [bool(value > self._threshold) for value in d2]

    @staticmethod
    def _distances(
        seq: np.ndarray,
        fit_rows: np.ndarray,
        floors: np.ndarray,
    ) -> Optional[np.ndarray]:
        """Squared Mahalanobis distances of ``seq`` under a fit on ``fit_rows``."""
        center = np.median(fit_rows, axis=0)
        cov = np.cov(fit_rows.T) + np.diag(floors**2)
        try:
            inv_cov = np.linalg.inv(cov)
        except np.linalg.LinAlgError:
            return None
        centered = seq - center
        return np.einsum("ij,jk,ik->i", centered, inv_cov, centered)

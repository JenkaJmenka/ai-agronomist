"""Bayesian Online Change-Point Detection (BOCPD) for level shifts (plan P3).

Complements CUSUM: CUSUM's fixed slack/decision constants (K, H) trade
detection delay for false-alarm rate at ONE assumed shift size, while
BOCPD maintains a posterior over "run length since the last change" and
reacts to any shift the Student-t predictive finds surprising.

Model: Normal-Gamma conjugate prior (unknown mean and variance) with a
constant hazard ``1/hazard_lambda``. The series is standardized by its own
median/MAD (floored) so priors are unit-free across sensors. A timestep is
flagged when the MAP run length collapses — the posterior decided a new
regime started.

Reference: Adams & MacKay (2007), "Bayesian Online Changepoint Detection".
"""

from __future__ import annotations

from typing import List

import numpy as np

from src.config import BOCPD_HAZARD_LAMBDA, BOCPD_MIN_RUN

# Standardization floor so near-constant series don't blow up the z-scale.
_MAD_FLOOR = 1e-6
# MAP run length at or below this after a collapse counts as "reset".
_RESET_RUN_LENGTH = 2
# Steps after a reset whose MAP run must keep growing from ~0 to confirm a
# real regime change. A transient outlier collapses the run for one step and
# then the MAP jumps back to the old (long) run — this filters those out.
_CONFIRM_STEPS = 2


class BOCPDDetector:
    """Flag level-shift change-points in a 1-D series."""

    def __init__(
        self,
        hazard_lambda: float = BOCPD_HAZARD_LAMBDA,
        min_run: int = BOCPD_MIN_RUN,
    ) -> None:
        if hazard_lambda <= 1:
            raise ValueError(f"hazard_lambda must be > 1, got {hazard_lambda}")
        if min_run < 2:
            raise ValueError(f"min_run must be >= 2, got {min_run}")
        self._hazard = 1.0 / hazard_lambda
        self._min_run = min_run

    def detect(self, series: np.ndarray) -> List[bool]:
        """Flag timesteps where the MAP run length collapses (new regime).

        Args:
            series: 1-D array of sensor readings.

        Returns:
            List of booleans, one per timestep. A flag at ``t`` means the
            posterior decided a change-point occurred at/just before ``t``
            after at least ``min_run`` stable steps, confirmed by the run
            length continuing to grow from ~0 (not snapping back).
        """
        x = np.asarray(series, dtype=float)
        n = len(x)
        flags = [False] * n
        if n < self._min_run + 1 or not np.isfinite(x).all():
            return flags

        map_runs = self._map_run_lengths(x)
        for t in range(n):
            if map_runs[t] is None:
                continue
            if (
                t >= 1
                and map_runs[t - 1] is not None
                and map_runs[t - 1] >= self._min_run
                and map_runs[t] <= _RESET_RUN_LENGTH
                and self._reset_confirmed(map_runs, t)
            ):
                flags[t] = True
        return flags

    def _reset_confirmed(self, map_runs, t: int) -> bool:
        """True if the MAP run keeps growing from ~0 after the reset at ``t``.

        A real change-point yields runs like 2, 3, 4, …; a transient outlier
        yields 2 then a snap back to the old long run.
        """
        for offset in range(1, _CONFIRM_STEPS + 1):
            idx = t + offset
            if idx >= len(map_runs) or map_runs[idx] is None:
                return True  # ran out of series — accept (end-of-window shift)
            if map_runs[idx] > _RESET_RUN_LENGTH + offset:
                return False
        return True

    def _map_run_lengths(self, x: np.ndarray):
        """MAP run length per timestep (None where the recursion restarted)."""
        n = len(x)

        # Unit-free standardization on the series' own robust scale.
        median = float(np.median(x))
        mad = float(np.median(np.abs(x - median)))
        z = (x - median) / max(mad, _MAD_FLOOR)

        # Normal-Gamma prior hyperparameters (weakly informative).
        mu0, kappa0, alpha0, beta0 = 0.0, 1.0, 1.0, 1.0

        # Run-length posterior and per-run sufficient statistics; index r
        # holds the hypothesis "current run is r steps old".
        run_probs = np.array([1.0])
        mu = np.array([mu0])
        kappa = np.array([kappa0])
        alpha = np.array([alpha0])
        beta = np.array([beta0])
        map_runs: list = []

        for t in range(n):
            # Student-t predictive probability of x_t under each run length.
            df = 2.0 * alpha
            scale = np.sqrt(beta * (kappa + 1.0) / (alpha * kappa))
            pred = _student_t_pdf(z[t], df=df, loc=mu, scale=scale)

            growth = run_probs * pred * (1.0 - self._hazard)
            change = float(np.sum(run_probs * pred * self._hazard))
            run_probs = np.concatenate(([change], growth))
            total = float(np.sum(run_probs))
            if total <= 0 or not np.isfinite(total):
                # Numerical underflow — restart the recursion.
                run_probs = np.array([1.0])
                mu, kappa, alpha, beta = (
                    np.array([mu0]), np.array([kappa0]),
                    np.array([alpha0]), np.array([beta0]),
                )
                map_runs.append(None)
                continue
            run_probs /= total

            # Posterior updates, shifted one slot (run length grew by one).
            kappa_new = kappa + 1.0
            mu_new = (kappa * mu + z[t]) / kappa_new
            alpha_new = alpha + 0.5
            beta_new = beta + 0.5 * kappa * (z[t] - mu) ** 2 / kappa_new
            mu = np.concatenate(([mu0], mu_new))
            kappa = np.concatenate(([kappa0], kappa_new))
            alpha = np.concatenate(([alpha0], alpha_new))
            beta = np.concatenate(([beta0], beta_new))

            map_runs.append(int(np.argmax(run_probs)))

        return map_runs


def _student_t_pdf(
    value: float,
    df: np.ndarray,
    loc: np.ndarray,
    scale: np.ndarray,
) -> np.ndarray:
    """Vectorized Student-t density (avoids a scipy dependency)."""
    from math import lgamma

    zval = (value - loc) / scale
    log_norm = (
        np.array([lgamma((d + 1.0) / 2.0) - lgamma(d / 2.0) for d in df])
        - 0.5 * np.log(df * np.pi)
        - np.log(scale)
    )
    log_pdf = log_norm - ((df + 1.0) / 2.0) * np.log1p(zval**2 / df)
    return np.exp(log_pdf)

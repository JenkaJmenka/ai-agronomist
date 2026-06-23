"""
Gas sensor anomaly detector for SCADA/greenhouse environments.

Detects statistical anomalies in gas sensor readings (C2H4, CO2, NO, NO2, NOx, CO)
using a combined z-score and IQR approach.  A baseline is first fitted from
historical readings; individual readings are then scored against it.
"""

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from src.config import GAS_SENSOR_COLUMNS as GAS_COLUMNS

# Minimum number of finite values required to compute a meaningful baseline.
_MIN_BASELINE_SAMPLES: int = 5

# Z-score thresholds for anomaly severity classification.
_Z_WARNING: float = 2.0
_Z_CRITICAL: float = 3.0

# IQR fence multiplier (Tukey's rule).
_IQR_FENCE: float = 1.5


@dataclass
class GasBaseline:
    """Statistical baseline for a single gas sensor column."""

    column: str
    mean: float
    std: float
    q25: float
    q75: float
    iqr: float
    n: int


@dataclass
class GasAnomaly:
    """Anomaly detection result for a single gas sensor reading."""

    is_anomaly: bool
    z_score: float
    # "z_score", "iqr", or "none"
    method: str
    # "normal" (|z|<2), "warning" (2<=|z|<3), "critical" (|z|>=3)
    severity: str


class GasAnomalyDetector:
    """
    Two-phase detector: fit() builds per-column baselines from a batch of
    historical readings; detect() scores individual readings against those
    baselines using z-score and IQR fences.
    """

    GAS_COLUMNS: list[str] = GAS_COLUMNS

    # ------------------------------------------------------------------
    # Private helpers (extracted to keep fit/detect under 50 lines)
    # ------------------------------------------------------------------

    def _collect_column_values(
        self, readings: list[dict]
    ) -> dict[str, list[float]]:
        """Accumulate per-column finite float values from raw readings.

        Args:
            readings: List of dicts mapping column names to numeric values.

        Returns:
            Mapping of column name → list of finite float values.
        """
        col_values: dict[str, list[float]] = {col: [] for col in GAS_COLUMNS}
        for row in readings:
            for col in GAS_COLUMNS:
                raw = row.get(col)
                if raw is None:
                    continue
                try:
                    fval = float(raw)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(fval):
                    col_values[col].append(fval)
        return col_values

    def _build_baseline(self, col: str, vals: list[float]) -> GasBaseline:
        """Compute statistical baseline for a single gas column.

        Args:
            col: Column name (e.g. ``"c2h4"``).
            vals: List of finite float values for the column.

        Returns:
            A ``GasBaseline`` with mean, std, quartiles, and IQR.
        """
        arr = np.array(vals, dtype=float)
        q25 = float(np.percentile(arr, 25))
        q75 = float(np.percentile(arr, 75))
        return GasBaseline(
            column=col,
            mean=float(np.mean(arr)),
            std=float(np.std(arr)),
            q25=q25,
            q75=q75,
            iqr=q75 - q25,
            n=len(vals),
        )

    def _score_column(self, value: float, baseline: GasBaseline) -> GasAnomaly:
        """Score a single reading against its column baseline.

        Uses combined z-score and IQR fence detection.

        Args:
            value: Current sensor reading (must be finite).
            baseline: Statistical baseline for this column.

        Returns:
            A ``GasAnomaly`` with anomaly flag, z-score, method, severity.
        """
        if baseline.std == 0.0:
            return GasAnomaly(
                is_anomaly=False, z_score=0.0, method="none", severity="normal"
            )

        z_score = abs(value - baseline.mean) / baseline.std
        z_anomaly = z_score > _Z_CRITICAL

        iqr_anomaly = False
        if baseline.iqr > 0.0:
            lower_fence = baseline.q25 - _IQR_FENCE * baseline.iqr
            upper_fence = baseline.q75 + _IQR_FENCE * baseline.iqr
            iqr_anomaly = value < lower_fence or value > upper_fence

        is_anomaly = z_anomaly or iqr_anomaly
        if z_anomaly:
            method = "z_score"
        elif iqr_anomaly:
            method = "iqr"
        else:
            method = "none"

        if z_score >= _Z_CRITICAL:
            severity = "critical"
        elif z_score >= _Z_WARNING:
            severity = "warning"
        else:
            severity = "normal"

        return GasAnomaly(
            is_anomaly=is_anomaly,
            z_score=z_score,
            method=method,
            severity=severity,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fit(self, readings: list[dict]) -> dict[str, GasBaseline]:
        """Build statistical baselines from a list of sensor data dicts.

        Args:
            readings: List of dicts mapping column names to numeric values
                      (e.g. ``[{"c2h4": 0.05, "no": 1.2}, ...]``).

        Returns:
            Mapping of column name → GasBaseline.  Columns with fewer than
            ``_MIN_BASELINE_SAMPLES`` finite values are omitted.
        """
        col_values = self._collect_column_values(readings)
        baselines: dict[str, GasBaseline] = {}
        for col, vals in col_values.items():
            if len(vals) < _MIN_BASELINE_SAMPLES:
                continue
            baselines[col] = self._build_baseline(col, vals)
        return baselines

    def detect(
        self,
        values: dict[str, float],
        baselines: dict[str, GasBaseline],
    ) -> dict[str, GasAnomaly]:
        """Score each column in *values* that has a matching baseline.

        Args:
            values: Dict of column name → current sensor value.
            baselines: Baseline stats returned by :meth:`fit`.

        Returns:
            Mapping of column name → GasAnomaly for every column that
            appears in both *values* and *baselines*.
        """
        results: dict[str, GasAnomaly] = {}
        for col, baseline in baselines.items():
            raw = values.get(col)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            results[col] = self._score_column(value, baseline)
        return results

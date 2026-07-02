"""Statistical cold-run anomaly detection on all climate CSV columns."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.config import (
    COLD_RUN_BASELINE_FRACTION,
    COLD_RUN_STAT_MIN_CONSECUTIVE,
    COLD_RUN_STAT_ROLLING_WINDOW,
    COLD_RUN_STAT_USE_IQR,
    COLD_RUN_STAT_Z_MIN,
)
from src.dashboard.components.sensor_columns import is_gas_column
from src.models.gas_anomaly_detector import (
    GasAnomaly,
    GasBaseline,
    _IQR_FENCE,
    _MIN_BASELINE_SAMPLES,
    _Z_CRITICAL,
    _Z_WARNING,
)

_METADATA_KEYS = frozenset({"timestamp", "source"})

# Actuators and diurnal channels produce false positives with rolling z-scores.
_COLD_RUN_STAT_EXCLUDE_SUBSTRINGS = (
    "light",
    "rain",
    "screen",
    "vent",
    "deficit",
)


def is_cold_run_stat_column(name: str) -> bool:
    """Continuous climate sensors suitable for rolling cold-run detection."""
    if name in _METADATA_KEYS or is_gas_column(name):
        return False
    norm = name.strip().lower()
    if any(token in norm for token in _COLD_RUN_STAT_EXCLUDE_SUBSTRINGS):
        return False
    return any(
        token in norm
        for token in ("temp", "humid", "humidity", "wind", "deficit")
    )


@dataclass(frozen=True)
class RollingAnomaly:
    """Rolling-window statistical anomaly for one sensor reading."""

    timestamp: str
    column: str
    value: float
    z_score: float
    severity: str
    method: str = "rolling_z_score"


class ColdRunStatisticalDetector:
    """Per-column z-score detection with optional IQR fences."""

    def __init__(
        self,
        *,
        z_min: float = COLD_RUN_STAT_Z_MIN,
        use_iqr: bool = COLD_RUN_STAT_USE_IQR,
        min_consecutive: int = COLD_RUN_STAT_MIN_CONSECUTIVE,
    ) -> None:
        self._z_min = z_min
        self._use_iqr = use_iqr
        self._min_consecutive = max(1, min_consecutive)

    def _collect_values(self, readings: list[dict[str, Any]]) -> dict[str, list[float]]:
        col_values: dict[str, list[float]] = {}
        for row in readings:
            for col, raw in row.items():
                if not is_cold_run_stat_column(col):
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    col_values.setdefault(col, []).append(value)
        return col_values

    def _build_baseline(self, col: str, vals: list[float]) -> GasBaseline:
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
        if baseline.std == 0.0:
            return GasAnomaly(
                is_anomaly=False, z_score=0.0, method="none", severity="normal"
            )
        z_score = abs(value - baseline.mean) / baseline.std
        z_anomaly = z_score >= self._z_min
        iqr_anomaly = False
        if self._use_iqr and baseline.iqr > 0.0:
            lower = baseline.q25 - _IQR_FENCE * baseline.iqr
            upper = baseline.q75 + _IQR_FENCE * baseline.iqr
            iqr_anomaly = value < lower or value > upper
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

    def fit(self, readings: list[dict[str, Any]]) -> dict[str, GasBaseline]:
        col_values = self._collect_values(readings)
        baselines: dict[str, GasBaseline] = {}
        for col, vals in col_values.items():
            if len(vals) < _MIN_BASELINE_SAMPLES:
                continue
            baselines[col] = self._build_baseline(col, vals)
        return baselines

    def detect(
        self,
        values: dict[str, Any],
        baselines: dict[str, GasBaseline],
    ) -> dict[str, GasAnomaly]:
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

    def detect_rolling(
        self,
        readings: list[dict[str, Any]],
        *,
        window: int = COLD_RUN_STAT_ROLLING_WINDOW,
        columns: list[str] | None = None,
    ) -> list[RollingAnomaly]:
        """Flag sudden deviations vs a recent rolling baseline (not seasonal drift)."""
        effective_window = _effective_rolling_window(len(readings), window)
        if effective_window <= 0 or len(readings) <= effective_window:
            return []

        df = pd.DataFrame(readings)
        if columns is None:
            columns = self._discover_columns(readings)
        anomalies: list[RollingAnomaly] = []
        min_window_points = max(5, effective_window // 4)

        if "timestamp" in df.columns:
            timestamps = df["timestamp"].fillna("unknown").astype(str).tolist()
        else:
            timestamps = ["unknown"] * len(df)

        for col in columns:
            if col not in df.columns:
                continue
            series = pd.to_numeric(df[col], errors='coerce')
            series = series.where(np.isfinite(series), np.nan)

            shifted = series.shift(1)
            rolling = shifted.rolling(window=effective_window, min_periods=min_window_points)
            rolling_mean = rolling.mean()
            rolling_std = rolling.std(ddof=0)

            with np.errstate(divide='ignore', invalid='ignore'):
                z_score_series = (series - rolling_mean).abs() / rolling_std

            is_anomaly_candidate = (
                series.notna() &
                rolling_mean.notna() &
                rolling_std.notna() &
                (rolling_std >= 1e-9) &
                (z_score_series >= self._z_min)
            )

            is_anomaly_candidate.iloc[:effective_window] = False

            streak_series = is_anomaly_candidate.groupby((~is_anomaly_candidate).cumsum()).cumsum()

            anomaly_indices = streak_series[streak_series >= self._min_consecutive].index

            for idx in anomaly_indices:
                val = series.iloc[idx]
                z_score = float(z_score_series.iloc[idx])

                if z_score >= _Z_CRITICAL:
                    severity = "critical"
                elif z_score >= _Z_WARNING:
                    severity = "warning"
                else:
                    severity = "normal"

                anomalies.append(
                    RollingAnomaly(
                        timestamp=timestamps[idx],
                        column=col,
                        value=float(val),
                        z_score=z_score,
                        severity=severity,
                    )
                )

        return anomalies

    def _discover_columns(self, readings: list[dict[str, Any]]) -> list[str]:
        columns: set[str] = set()
        for row in readings:
            for col in row:
                if is_cold_run_stat_column(col):
                    columns.add(col)
        return sorted(columns)

    def _column_series(
        self,
        readings: list[dict[str, Any]],
        column: str,
    ) -> tuple[np.ndarray, list[str]]:
        values: list[float] = []
        timestamps: list[str] = []
        for row in readings:
            timestamps.append(str(row.get("timestamp", "unknown")))
            raw = row.get(column)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                val = float("nan")
            values.append(val if math.isfinite(val) else float("nan"))
        return np.asarray(values, dtype=float), timestamps


def _effective_rolling_window(row_count: int, configured_window: int) -> int:
    """Scale rolling window down for short files; keep long-file window unchanged."""
    if row_count < 24:
        return 0
    adaptive = max(12, row_count // 20)
    return min(configured_window, adaptive)


def split_baseline_readings(
    readings: list[dict[str, Any]],
    *,
    fraction: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split readings into baseline fit slice and scoring slice by row order."""
    if not readings:
        return [], []
    frac = fraction if fraction is not None else COLD_RUN_BASELINE_FRACTION
    split_at = max(1, int(len(readings) * frac))
    return readings[:split_at], readings[split_at:]

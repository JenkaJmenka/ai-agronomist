"""Seasonal σ scaler for univariate detector limits.

``factor = clamp(σ_season / σ_global, SEASONAL_SCALE_MIN, SEASONAL_SCALE_MAX)``
with fallback 1.0 when sample count is below ``SEASONAL_MIN_SAMPLES``.
Manual overrides come from ``seasonal_overrides.{season}.detector_scale``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from src.config import (
    SEASONAL_MIN_SAMPLES,
    SEASONAL_SCALE_MAX,
    SEASONAL_SCALE_MIN,
)


@dataclass(frozen=True)
class SeasonStats:
    """Per-sensor seasonal summary statistics."""

    mean: float
    std: float
    median: float
    n: int


@dataclass
class SeasonalScaler:
    """Lookup σ_season / σ_global factors per season and sensor."""

    season_stats: dict[str, dict[str, SeasonStats]] = field(default_factory=dict)
    global_std: dict[str, float] = field(default_factory=dict)
    overrides: dict[str, float | None] = field(default_factory=dict)

    def factor(self, season: str, sensor: str) -> float:
        """Return the multiplicative scale for *sensor* in *season*."""
        override = self.overrides.get(season)
        if override is not None and override > 0:
            return _clamp(float(override))

        stats = self.season_stats.get(season, {}).get(sensor)
        g_std = self.global_std.get(sensor)
        if stats is None or g_std is None or g_std <= 0 or stats.n < SEASONAL_MIN_SAMPLES:
            return 1.0
        if stats.std <= 0:
            return 1.0
        return _clamp(stats.std / g_std)

    def factors_for_season(self, season: str) -> dict[str, float]:
        sensors = set(self.global_std) | set(self.season_stats.get(season, {}))
        return {s: self.factor(season, s) for s in sorted(sensors)}


def _clamp(value: float) -> float:
    return max(SEASONAL_SCALE_MIN, min(SEASONAL_SCALE_MAX, value))


def build_scaler_from_baselines(
    baselines: Mapping[str, Any],
    *,
    overrides: Mapping[str, Any] | None = None,
) -> SeasonalScaler:
    """Build a :class:`SeasonalScaler` from the seasonal baseline service dict.

    Expected shape::

        {
          "global": {"temperature": {"std": …}, …},
          "seasons": {
            "summer": {"temperature": {"mean":…,"std":…,"median":…,"n":…}, …},
            "winter": {…},
          },
        }
    """
    global_std: dict[str, float] = {}
    for sensor, stats in (baselines.get("global") or {}).items():
        try:
            global_std[sensor] = float(stats["std"])
        except (KeyError, TypeError, ValueError):
            continue

    season_stats: dict[str, dict[str, SeasonStats]] = {}
    for season, sensors in (baselines.get("seasons") or {}).items():
        season_stats[season] = {}
        for sensor, stats in (sensors or {}).items():
            try:
                season_stats[season][sensor] = SeasonStats(
                    mean=float(stats["mean"]),
                    std=float(stats["std"]),
                    median=float(stats["median"]),
                    n=int(stats["n"]),
                )
            except (KeyError, TypeError, ValueError):
                continue

    override_scales: dict[str, float | None] = {}
    for season, payload in (overrides or {}).items():
        if not isinstance(payload, Mapping):
            continue
        raw = payload.get("detector_scale")
        override_scales[str(season)] = float(raw) if raw is not None else None

    return SeasonalScaler(
        season_stats=season_stats,
        global_std=global_std,
        overrides=override_scales,
    )


# Process-local cache shared by the inference pipeline and config PUT fan-out.
_SEASONAL_SCALER_CACHE: dict[tuple[str, str, str], SeasonalScaler] = {}


def get_cached_seasonal_scaler(
    tenant_id: str,
    greenhouse_id: str,
    season: str,
) -> SeasonalScaler | None:
    return _SEASONAL_SCALER_CACHE.get((tenant_id, greenhouse_id, season))


def put_cached_seasonal_scaler(
    tenant_id: str,
    greenhouse_id: str,
    season: str,
    scaler: SeasonalScaler,
) -> None:
    _SEASONAL_SCALER_CACHE[(tenant_id, greenhouse_id, season)] = scaler


def clear_seasonal_scaler_cache(tenant_id: str | None = None) -> None:
    """Drop cached scalers — all tenants, or only *tenant_id*."""
    if tenant_id is None:
        _SEASONAL_SCALER_CACHE.clear()
        return
    for key in list(_SEASONAL_SCALER_CACHE):
        if key[0] == tenant_id:
            del _SEASONAL_SCALER_CACHE[key]

"""Setpoint-aware SCADA anomaly rules for greenhouse climate control.

Replaces static 20–30 °C “normal band” checks with climate-computer
setpoint tracking, RH / humidity-deficit dehumidification rules,
crop–air thermal divergence, and multi-hour sensor flatline detection.

All thresholds are sourced from ``src.config`` (no magic numbers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

from src.config import (
    DEHUMID_HD_CRIT_G_M3,
    FLATLINE_IDENTICAL_STEPS,
    FLATLINE_VARIANCE_WINDOW,
    OUTDOOR_TEMP_COLUMN_ALIASES,
    OVERHEAT_OUTDOOR_TEMP_C,
    OVERHEAT_SOLAR_WM2,
    PLANT_TEMP_COLUMN_ALIASES,
    RH_ABSOLUTE_MAX_PERCENT,
    RH_CRIT_PERCENT,
    SETPOINT_COLUMN_ALIASES,
    SETPOINT_TEMP_DELTA_C,
    SOLAR_COLUMN_ALIASES,
    THERMAL_DIVERGENCE_CRIT_C,
    THERMAL_DIVERGENCE_SEVERE_C,
)
from src.models.psychrometric_validator import calculate_humidity_deficit

# Canonical anomaly_type values (operator-facing schema).
UNDER_HEATING = "UNDER_HEATING"
OVER_HEATING = "OVER_HEATING"
DEHUMIDIFICATION_FAILURE = "DEHUMIDIFICATION_FAILURE"
HUMIDITY_SATURATION_RISK = "HUMIDITY_SATURATION_RISK"
THERMAL_DIVERGENCE = "THERMAL_DIVERGENCE"
SENSOR_FLATLINE = "SENSOR_FLATLINE"


@dataclass(frozen=True)
class ScadaAnomalyRecord:
    """One setpoint-aware / microclimate / sensor-health finding."""

    timestamp: str
    anomaly_type: str
    score: float
    affected_sensors: List[str]
    observed_values: Dict[str, float]
    reasoning: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "anomaly_type": self.anomaly_type,
            "score": float(self.score),
            "affected_sensors": list(self.affected_sensors),
            "observed_values": dict(self.observed_values),
            "reasoning": self.reasoning,
        }


@dataclass
class ClimateContext:
    """Optional climate-computer / microclimate scalars for the latest step."""

    t_setpoint: Optional[float] = None
    t_plant: Optional[float] = None
    t_outdoor: Optional[float] = None
    i_solar: Optional[float] = None
    # Optional full series for flatline on plant canopy.
    t_plant_series: Optional[np.ndarray] = field(default=None, repr=False)


def _alias_lookup(mapping: Mapping[str, Any], aliases: Sequence[str]) -> Optional[float]:
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for alias in aliases:
        if alias.lower() not in lowered:
            continue
        raw = lowered[alias.lower()]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if np.isfinite(value):
            return value
    return None


def climate_context_from_mapping(mapping: Optional[Mapping[str, Any]]) -> ClimateContext:
    """Build a :class:`ClimateContext` from a raw reading / extras dict."""
    if not mapping:
        return ClimateContext()
    return ClimateContext(
        t_setpoint=_alias_lookup(mapping, SETPOINT_COLUMN_ALIASES),
        t_plant=_alias_lookup(mapping, PLANT_TEMP_COLUMN_ALIASES),
        t_outdoor=_alias_lookup(mapping, OUTDOOR_TEMP_COLUMN_ALIASES),
        i_solar=_alias_lookup(mapping, SOLAR_COLUMN_ALIASES),
    )


def humidity_reasoning_text(rh: float) -> str:
    """Natural-language RH phrase — never 'at maximum' unless RH ≥ 98%."""
    if rh >= RH_ABSOLUTE_MAX_PERCENT:
        return f"Humidity at maximum ({rh:.1f}%)"
    return f"Humidity at {rh:.1f}%"


def _clamp01(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def _is_flatline(series: np.ndarray, *, window: int, identical_steps: int) -> bool:
    """True when recent samples are perfectly constant (frozen SCADA signal).

    Prefers ``identical_steps`` consecutive equals when the series is long
    enough (≈2 h at 5-min). Shorter pipeline windows (e.g. SEQ_LEN=20) still
    alarm when the *entire* available history is constant and at least
    ``window`` samples long.
    """
    if series.size < window:
        return False
    if not np.isfinite(series).all():
        return False
    required = identical_steps if series.size >= identical_steps else series.size
    tail = series[-required:]
    if float(np.ptp(tail)) > 0.0:
        return False
    var_window = series[-window:]
    return float(np.var(var_window)) == 0.0


class SetpointAwareScadaDetector:
    """Evaluate setpoint, RH, microclimate, and flatline rules on one sequence.

    ``sequence`` is denormalized physical units with columns
    ``[temperature, humidity, co2, light]`` (extra columns ignored).
    """

    def detect(
        self,
        sequence: np.ndarray,
        *,
        context: Optional[ClimateContext] = None,
        timestamp: Optional[datetime] = None,
    ) -> List[ScadaAnomalyRecord]:
        if sequence.ndim != 2 or sequence.shape[1] < 2:
            raise ValueError(
                f"Expected (seq_len, ≥2) denormalized array, got {sequence.shape}"
            )
        ctx = context or ClimateContext()
        ts = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
        ts_iso = ts.isoformat().replace("+00:00", "Z")

        t_gh = float(sequence[-1, 0])
        rh = float(sequence[-1, 1])
        findings: List[ScadaAnomalyRecord] = []

        findings.extend(self._temperature_rules(t_gh, ctx, ts_iso))
        findings.extend(self._humidity_rules(t_gh, rh, ts_iso))
        findings.extend(self._thermal_divergence(t_gh, ctx, ts_iso))
        findings.extend(self._flatline_rules(sequence, ctx, ts_iso))
        return findings

    def _temperature_rules(
        self, t_gh: float, ctx: ClimateContext, ts_iso: str
    ) -> List[ScadaAnomalyRecord]:
        if ctx.t_setpoint is None or not np.isfinite(ctx.t_setpoint):
            return []
        sp = float(ctx.t_setpoint)
        deficit = sp - t_gh
        excess = t_gh - sp
        out: List[ScadaAnomalyRecord] = []

        if deficit > SETPOINT_TEMP_DELTA_C:
            score = _clamp01(deficit / (SETPOINT_TEMP_DELTA_C * 3.0))
            out.append(
                ScadaAnomalyRecord(
                    timestamp=ts_iso,
                    anomaly_type=UNDER_HEATING,
                    score=score,
                    affected_sensors=["temperature"],
                    observed_values={"T_gh": t_gh, "T_setpoint": sp},
                    reasoning=(
                        f"Under-heating: greenhouse air {t_gh:.1f}°C is "
                        f"{deficit:.1f}°C below heating setpoint {sp:.1f}°C "
                        f"(margin {SETPOINT_TEMP_DELTA_C:.1f}°C)."
                    ),
                )
            )

        if excess > SETPOINT_TEMP_DELTA_C:
            solar_driven = (
                ctx.i_solar is not None
                and ctx.t_outdoor is not None
                and ctx.i_solar > OVERHEAT_SOLAR_WM2
                and ctx.t_outdoor > OVERHEAT_OUTDOOR_TEMP_C
            )
            if not solar_driven:
                observed: Dict[str, float] = {"T_gh": t_gh, "T_setpoint": sp}
                if ctx.t_outdoor is not None:
                    observed["T_outdoor"] = float(ctx.t_outdoor)
                if ctx.i_solar is not None:
                    observed["I_solar"] = float(ctx.i_solar)
                score = _clamp01(excess / (SETPOINT_TEMP_DELTA_C * 3.0))
                out.append(
                    ScadaAnomalyRecord(
                        timestamp=ts_iso,
                        anomaly_type=OVER_HEATING,
                        score=score,
                        affected_sensors=["temperature"],
                        observed_values=observed,
                        reasoning=(
                            f"Over-heating: greenhouse air {t_gh:.1f}°C is "
                            f"{excess:.1f}°C above heating setpoint {sp:.1f}°C "
                            f"(margin {SETPOINT_TEMP_DELTA_C:.1f}°C)."
                        ),
                    )
                )
        return out

    def _humidity_rules(
        self, t_gh: float, rh: float, ts_iso: str
    ) -> List[ScadaAnomalyRecord]:
        if not np.isfinite(rh) or rh < RH_CRIT_PERCENT:
            return []
        try:
            hd = float(calculate_humidity_deficit(t_gh, rh))
        except ValueError:
            return []

        rh_phrase = humidity_reasoning_text(rh)
        observed = {"T_gh": t_gh, "RH": rh, "HD": hd}
        out: List[ScadaAnomalyRecord] = []

        # Always surface saturation risk when RH approaches dew-point.
        sat_score = _clamp01((rh - RH_CRIT_PERCENT) / (100.0 - RH_CRIT_PERCENT) + 0.4)
        out.append(
            ScadaAnomalyRecord(
                timestamp=ts_iso,
                anomaly_type=HUMIDITY_SATURATION_RISK,
                score=sat_score,
                affected_sensors=["humidity"],
                observed_values=observed,
                reasoning=(
                    f"{rh_phrase} (≥ {RH_CRIT_PERCENT:.0f}% critical) — "
                    f"moisture saturation risk near dew point."
                ),
            )
        )

        if hd <= DEHUMID_HD_CRIT_G_M3:
            fail_score = _clamp01(0.7 + (RH_CRIT_PERCENT - hd) * 0.01)
            out.append(
                ScadaAnomalyRecord(
                    timestamp=ts_iso,
                    anomaly_type=DEHUMIDIFICATION_FAILURE,
                    score=fail_score,
                    affected_sensors=["humidity"],
                    observed_values=observed,
                    reasoning=(
                        f"Dehumidification failure: {rh_phrase} with humidity "
                        f"deficit {hd:.2f} g/m³ ≤ {DEHUMID_HD_CRIT_G_M3:.1f} g/m³."
                    ),
                )
            )
        return out

    def _thermal_divergence(
        self, t_gh: float, ctx: ClimateContext, ts_iso: str
    ) -> List[ScadaAnomalyRecord]:
        if ctx.t_plant is None or not np.isfinite(ctx.t_plant):
            return []
        t_plant = float(ctx.t_plant)
        delta = abs(t_plant - t_gh)
        if delta <= THERMAL_DIVERGENCE_CRIT_C:
            return []
        severe = delta >= THERMAL_DIVERGENCE_SEVERE_C
        score = _clamp01(delta / THERMAL_DIVERGENCE_SEVERE_C)
        severity = "severe" if severe else "elevated"
        return [
            ScadaAnomalyRecord(
                timestamp=ts_iso,
                anomaly_type=THERMAL_DIVERGENCE,
                score=score,
                affected_sensors=["temperature", "plant_temp"],
                observed_values={"T_gh": t_gh, "T_plant": t_plant},
                reasoning=(
                    f"Thermal divergence ({severity}): |T_plant − T_gh| = "
                    f"{delta:.1f}°C (T_plant={t_plant:.1f}°C, T_gh={t_gh:.1f}°C); "
                    f"crit={THERMAL_DIVERGENCE_CRIT_C:.1f}°C."
                ),
            )
        ]

    def _flatline_rules(
        self,
        sequence: np.ndarray,
        ctx: ClimateContext,
        ts_iso: str,
    ) -> List[ScadaAnomalyRecord]:
        out: List[ScadaAnomalyRecord] = []
        channels = [
            ("temperature", sequence[:, 0], "T_gh"),
            ("humidity", sequence[:, 1], "RH"),
        ]
        if ctx.t_plant_series is not None and ctx.t_plant_series.size:
            channels.append(("plant_temp", np.asarray(ctx.t_plant_series, dtype=float), "T_plant"))
        elif ctx.t_plant is not None:
            # Single plant sample cannot prove flatline; skip.
            pass

        for sensor, series, value_key in channels:
            if not _is_flatline(
                series,
                window=FLATLINE_VARIANCE_WINDOW,
                identical_steps=FLATLINE_IDENTICAL_STEPS,
            ):
                continue
            last = float(series[-1])
            out.append(
                ScadaAnomalyRecord(
                    timestamp=ts_iso,
                    anomaly_type=SENSOR_FLATLINE,
                    score=1.0,
                    affected_sensors=[sensor],
                    observed_values={value_key: last},
                    reasoning=(
                        f"Sensor flatline anomaly: {sensor} constant at {last:.2f} "
                        f"for ≥ {FLATLINE_IDENTICAL_STEPS} steps "
                        f"(~2 h at 5-min sampling); "
                        f"rolling variance over {FLATLINE_VARIANCE_WINDOW} = 0."
                    ),
                )
            )
        return out

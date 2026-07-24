"""Operator crop-band breach detector (day/night min–max categorical anomalies).

Separate from :class:`SetpointAwareScadaDetector`, which tracks climate-computer
scalar setpoints from telemetry.  This detector uses the tenant's operational
config bands (tomato/cucumber profiles) and shared Magnus VPD.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import numpy as np

from src.config import BAND_BREACH_DEBOUNCE_K, SENSOR_NAMES
from src.models.psychrometric_validator import calculate_saturation_vapor_pressure
from src.models.setpoint_scada_detector import ScadaAnomalyRecord

TEMP_BAND_HIGH = "TEMP_BAND_HIGH"
TEMP_BAND_LOW = "TEMP_BAND_LOW"
VPD_BAND_HIGH = "VPD_BAND_HIGH"
VPD_BAND_LOW = "VPD_BAND_LOW"
HUMIDITY_BAND_HIGH = "HUMIDITY_BAND_HIGH"
HUMIDITY_BAND_LOW = "HUMIDITY_BAND_LOW"
CO2_BAND_HIGH = "CO2_BAND_HIGH"
CO2_BAND_LOW = "CO2_BAND_LOW"

# anomaly_type → rule_vote key used by comprehensive_pipeline fusion.
BAND_TYPE_TO_VOTE: dict[str, str] = {
    TEMP_BAND_HIGH: "temp_band_high",
    TEMP_BAND_LOW: "temp_band_low",
    VPD_BAND_HIGH: "vpd_band_high",
    VPD_BAND_LOW: "vpd_band_low",
    HUMIDITY_BAND_HIGH: "humidity_band_high",
    HUMIDITY_BAND_LOW: "humidity_band_low",
    CO2_BAND_HIGH: "co2_band_high",
    CO2_BAND_LOW: "co2_band_low",
}

_CHANNEL_SENSOR: dict[str, str] = {
    "temperature": "temperature",
    "humidity": "humidity",
    "co2": "co2",
    "vpd": "temperature",  # VPD derived; attribute to air temp for fusion
}


def _is_day(hour: int, day_window: Mapping[str, Any]) -> bool:
    start = int(day_window.get("start_hour", 6))
    end = int(day_window.get("end_hour", 22))
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    # Overnight window (e.g. 22 → 6): day spans midnight.
    return hour >= start or hour < end


def _pick_band(
    setpoints: Mapping[str, Any],
    channel: str,
    *,
    is_day: bool,
) -> tuple[float, float] | None:
    entry = setpoints.get(channel)
    if not isinstance(entry, Mapping):
        return None
    band = entry.get("day" if is_day else "night")
    if band is None and not is_day:
        band = entry.get("day")
    if not (isinstance(band, (list, tuple)) and len(band) == 2):
        return None
    try:
        lo, hi = float(band[0]), float(band[1])
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(lo) and np.isfinite(hi)) or lo >= hi:
        return None
    return lo, hi


def _compute_vpd(temp_c: float, rh_percent: float) -> float:
    """VPD in kPa from air temperature and RH."""
    es = calculate_saturation_vapor_pressure(temp_c)
    ea = es * (rh_percent / 100.0)
    return max(0.0, es - ea)


def _channel_index(name: str) -> int | None:
    try:
        return list(SENSOR_NAMES).index(name)
    except ValueError:
        return None


def _debounce_mean(series: np.ndarray, k: int) -> float | None:
    if series.size == 0:
        return None
    window = series[-k:] if series.size >= k else series
    finite = window[np.isfinite(window)]
    if finite.size == 0:
        return None
    return float(np.mean(finite))


class BandBreachDetector:
    """Detect last-k mean breaches of operator day/night crop bands."""

    def __init__(self, debounce_k: int = BAND_BREACH_DEBOUNCE_K) -> None:
        self._k = max(1, debounce_k)

    def detect(
        self,
        denormed_window: np.ndarray,
        config: Mapping[str, Any],
        timestamp: datetime | str | None = None,
    ) -> list[ScadaAnomalyRecord]:
        """Return categorical band-breach findings for the latest step."""
        if denormed_window.ndim != 2 or denormed_window.shape[0] == 0:
            return []

        ts = _normalize_ts(timestamp)
        hour = _hour_utc(ts)
        is_day = _is_day(hour, config.get("day_window") or {})
        setpoints = config.get("setpoints") or {}
        findings: list[ScadaAnomalyRecord] = []

        # Scalar channels present in the model window.
        for channel, high_type, low_type in (
            ("temperature", TEMP_BAND_HIGH, TEMP_BAND_LOW),
            ("humidity", HUMIDITY_BAND_HIGH, HUMIDITY_BAND_LOW),
            ("co2", CO2_BAND_HIGH, CO2_BAND_LOW),
        ):
            idx = _channel_index(channel)
            if idx is None or idx >= denormed_window.shape[1]:
                continue
            band = _pick_band(setpoints, channel, is_day=is_day)
            if band is None:
                continue
            value = _debounce_mean(denormed_window[:, idx], self._k)
            if value is None:
                continue
            finding = _compare_band(
                channel=channel,
                value=value,
                band=band,
                high_type=high_type,
                low_type=low_type,
                timestamp=ts,
                phase="day" if is_day else "night",
            )
            if finding is not None:
                findings.append(finding)

        # Derived VPD from temp + humidity.
        t_idx = _channel_index("temperature")
        h_idx = _channel_index("humidity")
        vpd_band = _pick_band(setpoints, "vpd", is_day=is_day)
        if (
            vpd_band is not None
            and t_idx is not None
            and h_idx is not None
            and t_idx < denormed_window.shape[1]
            and h_idx < denormed_window.shape[1]
        ):
            t_mean = _debounce_mean(denormed_window[:, t_idx], self._k)
            h_mean = _debounce_mean(denormed_window[:, h_idx], self._k)
            if t_mean is not None and h_mean is not None:
                vpd = _compute_vpd(t_mean, h_mean)
                finding = _compare_band(
                    channel="vpd",
                    value=vpd,
                    band=vpd_band,
                    high_type=VPD_BAND_HIGH,
                    low_type=VPD_BAND_LOW,
                    timestamp=ts,
                    phase="day" if is_day else "night",
                    affected=["temperature", "humidity"],
                    observed={"vpd": vpd, "temperature": t_mean, "humidity": h_mean},
                )
                if finding is not None:
                    findings.append(finding)

        return findings


def _compare_band(
    *,
    channel: str,
    value: float,
    band: tuple[float, float],
    high_type: str,
    low_type: str,
    timestamp: str,
    phase: str,
    affected: Sequence[str] | None = None,
    observed: Mapping[str, float] | None = None,
) -> ScadaAnomalyRecord | None:
    lo, hi = band
    sensors = list(affected) if affected is not None else [channel]
    obs = dict(observed) if observed is not None else {channel: value}
    if value > hi:
        return ScadaAnomalyRecord(
            timestamp=timestamp,
            anomaly_type=high_type,
            score=float(min(1.0, (value - hi) / max(hi - lo, 1e-6))),
            affected_sensors=list(sensors),
            observed_values=dict(obs),
            reasoning=(
                f"{channel} {phase} mean {value:.3g} exceeds configured max {hi:.3g}"
            ),
        )
    if value < lo:
        return ScadaAnomalyRecord(
            timestamp=timestamp,
            anomaly_type=low_type,
            score=float(min(1.0, (lo - value) / max(hi - lo, 1e-6))),
            affected_sensors=list(sensors),
            observed_values=dict(obs),
            reasoning=(
                f"{channel} {phase} mean {value:.3g} below configured min {lo:.3g}"
            ),
        )
    return None


def _normalize_ts(timestamp: datetime | str | None) -> str:
    if timestamp is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.isoformat()
    return str(timestamp)


def _hour_utc(ts: str) -> int:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 12
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).hour)

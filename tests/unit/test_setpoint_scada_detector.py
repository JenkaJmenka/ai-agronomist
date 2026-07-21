"""Unit tests for setpoint-aware SCADA anomaly rules."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from src.config import (
    DEHUMID_HD_CRIT_G_M3,
    FLATLINE_IDENTICAL_STEPS,
    RH_ABSOLUTE_MAX_PERCENT,
    RH_CRIT_PERCENT,
    SETPOINT_TEMP_DELTA_C,
    THERMAL_DIVERGENCE_CRIT_C,
    THERMAL_DIVERGENCE_SEVERE_C,
)
from src.models.setpoint_scada_detector import (
    DEHUMIDIFICATION_FAILURE,
    HUMIDITY_SATURATION_RISK,
    OVER_HEATING,
    SENSOR_FLATLINE,
    THERMAL_DIVERGENCE,
    UNDER_HEATING,
    ClimateContext,
    SetpointAwareScadaDetector,
    climate_context_from_mapping,
    humidity_reasoning_text,
)


def _denorm_seq(
    *,
    temp: float = 22.0,
    hum: float = 60.0,
    co2: float = 400.0,
    light: float = 300.0,
    seq_len: int = 20,
) -> np.ndarray:
    return np.tile([temp, hum, co2, light], (seq_len, 1)).astype(float)


class TestHumidityReasoningText:
    def test_never_says_maximum_below_98(self) -> None:
        text = humidity_reasoning_text(RH_CRIT_PERCENT)
        assert "at maximum" not in text.lower()
        assert f"{RH_CRIT_PERCENT:.1f}" in text

    def test_says_maximum_at_or_above_98(self) -> None:
        text = humidity_reasoning_text(RH_ABSOLUTE_MAX_PERCENT)
        assert "at maximum" in text.lower()


class TestClimateContextAliases:
    def test_resolves_heating_setpoint_alias(self) -> None:
        ctx = climate_context_from_mapping(
            {"calculated_heating_temp": 21.0, "measured_plant_temp": 18.0}
        )
        assert ctx.t_setpoint == 21.0
        assert ctx.t_plant == 18.0


class TestSetpointTemperatureRules:
    def test_under_heating_when_below_setpoint_margin(self) -> None:
        detector = SetpointAwareScadaDetector()
        t_sp = 22.0
        t_gh = t_sp - SETPOINT_TEMP_DELTA_C - 1.0
        findings = detector.detect(
            _denorm_seq(temp=t_gh, hum=60.0),
            context=ClimateContext(t_setpoint=t_sp),
            timestamp=datetime(2026, 7, 21, tzinfo=timezone.utc),
        )
        types = {f.anomaly_type for f in findings}
        assert UNDER_HEATING in types
        under = next(f for f in findings if f.anomaly_type == UNDER_HEATING)
        assert under.observed_values["T_setpoint"] == t_sp
        assert "below heating setpoint" in under.reasoning

    def test_no_under_heating_without_setpoint(self) -> None:
        detector = SetpointAwareScadaDetector()
        findings = detector.detect(_denorm_seq(temp=10.0, hum=60.0))
        assert all(f.anomaly_type != UNDER_HEATING for f in findings)

    def test_over_heating_suppressed_under_solar_gain(self) -> None:
        detector = SetpointAwareScadaDetector()
        t_sp = 22.0
        findings = detector.detect(
            _denorm_seq(temp=t_sp + SETPOINT_TEMP_DELTA_C + 3.0),
            context=ClimateContext(
                t_setpoint=t_sp, t_outdoor=28.0, i_solar=500.0
            ),
        )
        assert all(f.anomaly_type != OVER_HEATING for f in findings)

    def test_over_heating_fires_without_solar_excuse(self) -> None:
        detector = SetpointAwareScadaDetector()
        t_sp = 22.0
        findings = detector.detect(
            _denorm_seq(temp=t_sp + SETPOINT_TEMP_DELTA_C + 3.0),
            context=ClimateContext(t_setpoint=t_sp, t_outdoor=15.0, i_solar=50.0),
        )
        assert any(f.anomaly_type == OVER_HEATING for f in findings)


class TestHumidityRules:
    def test_saturation_and_dehumid_failure(self) -> None:
        detector = SetpointAwareScadaDetector()
        # High RH + low HD at cool-ish air → dehumid failure.
        findings = detector.detect(_denorm_seq(temp=20.0, hum=97.0))
        types = {f.anomaly_type for f in findings}
        assert HUMIDITY_SATURATION_RISK in types
        assert DEHUMIDIFICATION_FAILURE in types
        sat = next(f for f in findings if f.anomaly_type == HUMIDITY_SATURATION_RISK)
        assert "at maximum" not in sat.reasoning.lower()
        assert sat.observed_values["RH"] == 97.0
        assert sat.observed_values["HD"] <= DEHUMID_HD_CRIT_G_M3 + 1e-6

    def test_saturation_only_when_hd_still_healthy(self) -> None:
        detector = SetpointAwareScadaDetector()
        # Hot dry-ish air at RH just above crit can still have HD >> 0.5.
        findings = detector.detect(_denorm_seq(temp=35.0, hum=RH_CRIT_PERCENT))
        types = {f.anomaly_type for f in findings}
        assert HUMIDITY_SATURATION_RISK in types
        # May or may not fail dehumid depending on Tetens HD; if HD > crit, no failure.
        dehum = [f for f in findings if f.anomaly_type == DEHUMIDIFICATION_FAILURE]
        if dehum:
            assert dehum[0].observed_values["HD"] <= DEHUMID_HD_CRIT_G_M3


class TestThermalDivergence:
    def test_crit_and_severe(self) -> None:
        detector = SetpointAwareScadaDetector()
        t_gh = 22.0
        findings = detector.detect(
            _denorm_seq(temp=t_gh),
            context=ClimateContext(t_plant=t_gh + THERMAL_DIVERGENCE_CRIT_C + 0.5),
        )
        assert any(f.anomaly_type == THERMAL_DIVERGENCE for f in findings)
        severe = detector.detect(
            _denorm_seq(temp=t_gh),
            context=ClimateContext(t_plant=t_gh + THERMAL_DIVERGENCE_SEVERE_C),
        )
        msg = next(f for f in severe if f.anomaly_type == THERMAL_DIVERGENCE)
        assert "severe" in msg.reasoning


class TestSensorFlatline:
    def test_constant_series_flagged(self) -> None:
        detector = SetpointAwareScadaDetector()
        seq = _denorm_seq(temp=22.0, hum=55.0, seq_len=max(20, FLATLINE_IDENTICAL_STEPS))
        findings = detector.detect(seq)
        types = {f.anomaly_type for f in findings}
        assert SENSOR_FLATLINE in types
        sensors = {
            s
            for f in findings
            if f.anomaly_type == SENSOR_FLATLINE
            for s in f.affected_sensors
        }
        assert "temperature" in sensors
        assert "humidity" in sensors

    def test_varying_series_not_flatlined(self) -> None:
        detector = SetpointAwareScadaDetector()
        seq = _denorm_seq(seq_len=24)
        seq[:, 0] = np.linspace(20.0, 25.0, 24)
        seq[:, 1] = np.linspace(50.0, 70.0, 24)
        findings = detector.detect(seq, context=ClimateContext(t_setpoint=22.0))
        assert all(f.anomaly_type != SENSOR_FLATLINE for f in findings)


class TestRecordSchema:
    def test_to_dict_shape(self) -> None:
        detector = SetpointAwareScadaDetector()
        findings = detector.detect(
            _denorm_seq(temp=18.0),
            context=ClimateContext(t_setpoint=22.0),
        )
        under = next(f for f in findings if f.anomaly_type == UNDER_HEATING)
        payload = under.to_dict()
        assert set(payload) >= {
            "timestamp",
            "anomaly_type",
            "score",
            "affected_sensors",
            "observed_values",
            "reasoning",
        }
        assert 0.0 <= payload["score"] <= 1.0

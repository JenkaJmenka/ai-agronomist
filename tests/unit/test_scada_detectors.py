"""Unit tests for SCADA research-gap detector components."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from src.models.bias_detector import BiasOffsetDetector
from src.models.ewma_tracker import EWMATracker
from src.models.fault_classifier import FaultClassifier, FaultType
from src.models.gas_anomaly_detector import GasAnomalyDetector
from src.models.physics_validator import PhysicsValidator, ValidationResult
from src.models.pot_threshold import POTThresholdFitter
from src.models.stuck_fault_detector import StuckFaultDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seq(temp=25.0, hum=65.0, co2=400.0, light=300.0, seq_len=20) -> np.ndarray:
    """Return a constant-value (seq_len, 4) sequence."""
    return np.tile([temp, hum, co2, light], (seq_len, 1)).astype(float)


_RANGES = {
    "temperature": (20.0, 30.0),
    "humidity": (50.0, 80.0),
    "co2": (300.0, 500.0),
    "light": (0.0, 1000.0),
}


# ---------------------------------------------------------------------------
# FaultClassifier
# ---------------------------------------------------------------------------

class TestFaultClassifier:
    def test_bias_flag_takes_priority(self) -> None:
        result = FaultClassifier.classify("spike", {}, bias_flag=True)
        assert result == FaultType.BIAS_OFFSET.value

    def test_stuck_saturation_pattern(self) -> None:
        assert FaultClassifier.classify("saturation", {}) == FaultType.STUCK_FAULT.value
        assert FaultClassifier.classify("stuck_sensor", {}) == FaultType.STUCK_FAULT.value

    def test_spike_pattern(self) -> None:
        assert FaultClassifier.classify("spike", {}) == FaultType.SPIKE_FAULT.value

    def test_drift_pattern(self) -> None:
        assert FaultClassifier.classify("drift", {}) == FaultType.CALIBRATION_DRIFT.value

    def test_high_noise_accuracy_decline(self) -> None:
        result = FaultClassifier.classify("unknown", {"noise_level": "high"})
        assert result == FaultType.ACCURACY_DECLINE.value

    def test_default_unknown_fault(self) -> None:
        assert FaultClassifier.classify("unknown", {}) == FaultType.UNKNOWN.value

    def test_case_insensitive_pattern(self) -> None:
        assert FaultClassifier.classify("DRIFT", {}) == FaultType.CALIBRATION_DRIFT.value


# ---------------------------------------------------------------------------
# StuckFaultDetector
# ---------------------------------------------------------------------------

class TestStuckFaultDetector:
    def test_constant_sequence_is_stuck(self) -> None:
        detector = StuckFaultDetector(variance_threshold=1e-4)
        seq = _seq(temp=25.0, hum=65.0, co2=400.0, light=300.0)
        result = detector.detect(seq)
        assert all(result.values()), "All sensors should be flagged stuck for constant sequence"

    def test_varying_sequence_not_stuck(self) -> None:
        detector = StuckFaultDetector()
        rng = np.random.default_rng(0)
        seq = rng.normal(loc=[25, 65, 400, 300], scale=[1, 2, 10, 50], size=(20, 4))
        result = detector.detect(seq)
        assert not any(result.values()), "Normal variance sequence should not be stuck"

    def test_single_sensor_stuck(self) -> None:
        detector = StuckFaultDetector(variance_threshold=1e-4)
        rng = np.random.default_rng(1)
        seq = rng.normal(loc=[25, 65, 400, 300], scale=[1, 2, 10, 50], size=(20, 4))
        seq[:, 0] = 25.0  # freeze temperature only
        result = detector.detect(seq)
        assert result["temperature"] is True
        assert result["humidity"] is False

    def test_rejects_1d_input(self) -> None:
        detector = StuckFaultDetector()
        with pytest.raises(ValueError, match="2D"):
            detector.detect(np.array([1.0, 2.0, 3.0]))

    def test_returns_sensor_names(self) -> None:
        detector = StuckFaultDetector()
        result = detector.detect(_seq())
        assert set(result.keys()) == {"temperature", "humidity", "co2", "light"}


# ---------------------------------------------------------------------------
# BiasOffsetDetector
# ---------------------------------------------------------------------------

class TestBiasOffsetDetector:
    def test_no_bias_at_midpoint(self) -> None:
        detector = BiasOffsetDetector(_RANGES)
        seq = _seq(temp=25.0, hum=65.0, co2=400.0, light=500.0)
        result = detector.detect(seq)
        assert all(v == 0.0 for v in result.values()), "Midpoint should yield zero bias"

    def test_large_temperature_bias(self) -> None:
        detector = BiasOffsetDetector(_RANGES)
        # temperature range 20-30, midpoint 25, span 10
        # value 5 → deviation |5-25|=20 > 0.3*10=3 → flagged
        seq = _seq(temp=5.0)
        result = detector.detect(seq)
        assert result["temperature"] > 0.0

    def test_small_bias_not_flagged(self) -> None:
        detector = BiasOffsetDetector(_RANGES)
        # midpoint 25, 0.3*span = 3; value 26 → deviation 1 < 3
        seq = _seq(temp=26.0)
        result = detector.detect(seq)
        assert result["temperature"] == 0.0

    def test_missing_range_returns_zero(self) -> None:
        detector = BiasOffsetDetector({"temperature": (20.0, 30.0)})
        seq = _seq()
        result = detector.detect(seq)
        assert result["humidity"] == 0.0

    def test_rejects_1d_input(self) -> None:
        detector = BiasOffsetDetector(_RANGES)
        with pytest.raises(ValueError, match="2D"):
            detector.detect(np.array([1.0, 2.0]))


# ---------------------------------------------------------------------------
# EWMATracker
# ---------------------------------------------------------------------------

class TestEWMATracker:
    def test_inf_threshold_during_warmup(self) -> None:
        tracker = EWMATracker(alpha=0.1, k=3.0)
        for _ in range(9):
            tracker.update(0.5)
        assert tracker.get_threshold() == float("inf")

    def test_finite_threshold_after_min_obs(self) -> None:
        tracker = EWMATracker(alpha=0.1, k=3.0)
        for _ in range(10):
            tracker.update(0.5)
        assert tracker.get_threshold() < float("inf")

    def test_is_anomaly_true_for_large_score(self) -> None:
        tracker = EWMATracker(alpha=0.1, k=0.0)  # k=0 → threshold = mean
        for _ in range(20):
            tracker.update(0.1)
        assert tracker.is_anomaly(10.0)

    def test_serialization_roundtrip(self) -> None:
        tracker = EWMATracker(alpha=0.2, k=2.5)
        for i in range(15):
            tracker.update(float(i) * 0.1)
        state = tracker.to_dict()
        restored = EWMATracker.from_dict(state)
        assert restored.get_threshold() == pytest.approx(tracker.get_threshold())

    def test_invalid_alpha_raises(self) -> None:
        with pytest.raises(ValueError):
            EWMATracker(alpha=0.0)

    def test_nonfinite_score_raises(self) -> None:
        tracker = EWMATracker()
        with pytest.raises(ValueError, match="finite"):
            tracker.update(float("nan"))


# ---------------------------------------------------------------------------
# POTThresholdFitter
# ---------------------------------------------------------------------------

class TestPOTThresholdFitter:
    def test_inf_threshold_with_insufficient_data(self) -> None:
        fitter = POTThresholdFitter()
        fitter.update(0.5)
        assert fitter.get_threshold() == float("inf")

    def test_finite_threshold_after_buffer_fill(self) -> None:
        fitter = POTThresholdFitter(q=0.9, min_peaks=2, buffer_size=100)
        rng = np.random.default_rng(42)
        for val in rng.exponential(scale=0.3, size=60):
            fitter.update(float(val))
        assert fitter.get_threshold() < float("inf")

    def test_fit_returns_float(self) -> None:
        fitter = POTThresholdFitter(min_peaks=2)
        rng = np.random.default_rng(0)
        for val in rng.exponential(scale=0.5, size=50):
            fitter._buffer.append(float(val))
        result = fitter.fit()
        assert isinstance(result, float) and result > 0

    def test_serialization_roundtrip(self) -> None:
        fitter = POTThresholdFitter(q=0.85, fpr=0.01)
        for val in np.linspace(0.1, 1.0, 30):
            fitter.update(float(val))
        state = fitter.to_dict()
        restored = POTThresholdFitter.from_dict(state)
        assert restored.get_threshold() == pytest.approx(fitter.get_threshold())

    def test_fallback_to_quantile_with_few_peaks(self) -> None:
        fitter = POTThresholdFitter(min_peaks=1000)
        rng = np.random.default_rng(1)
        for val in rng.exponential(scale=0.3, size=50):
            fitter._buffer.append(float(val))
        result = fitter.fit()
        assert isinstance(result, float)


# ---------------------------------------------------------------------------
# PhysicsValidator
# ---------------------------------------------------------------------------

class TestPhysicsValidator:
    _NOW = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)  # noon UTC

    def _ramp(self, start, end, col: int, base_seq: np.ndarray) -> np.ndarray:
        seq = base_seq.copy()
        seq[:, col] = np.linspace(start, end, 20)
        return seq

    def test_no_rule_fires_for_normal_sequence(self) -> None:
        seq = _seq()
        result = PhysicsValidator.validate(seq, self._NOW)
        assert not result.suppressed
        assert not result.confirmed
        assert result.reason == ""

    def test_psychrometric_impossibility_confirmed(self) -> None:
        # Both temperature and humidity rising sharply
        seq = _seq()
        seq[:, 0] = np.linspace(20, 40, 20)  # temp rising +1 deg/step
        seq[:, 1] = np.linspace(50, 70, 20)  # humidity rising +1 %/step
        result = PhysicsValidator.validate(seq, self._NOW)
        assert result.confirmed
        assert result.reason == "psychrometric_impossibility"

    def test_nocturnal_light_confirmed(self) -> None:
        night_ts = datetime(2026, 6, 20, 2, 0, tzinfo=timezone.utc)
        seq = _seq(light=500.0)  # high light at 2 AM
        result = PhysicsValidator.validate(seq, night_ts)
        assert result.confirmed
        assert result.reason == "nocturnal_light"

    def test_co2_too_low_confirmed(self) -> None:
        seq = _seq(co2=100.0)  # below 200 ppm
        result = PhysicsValidator.validate(seq, self._NOW)
        assert result.confirmed
        assert result.reason == "co2_out_of_range"

    def test_co2_too_high_confirmed(self) -> None:
        seq = _seq(co2=2500.0)
        result = PhysicsValidator.validate(seq, self._NOW)
        assert result.confirmed
        assert result.reason == "co2_out_of_range"

    def test_temperature_out_of_range_confirmed(self) -> None:
        seq = _seq(temp=50.0)
        result = PhysicsValidator.validate(seq, self._NOW)
        assert result.confirmed
        assert result.reason == "temperature_out_of_range"

    def test_short_sequence_returns_no_match(self) -> None:
        seq = np.zeros((1, 3))  # fewer than 4 columns
        result = PhysicsValidator.validate(seq, self._NOW)
        assert not result.confirmed and not result.suppressed

    def test_psychrometric_not_triggered_when_humidity_falls(self) -> None:
        seq = _seq()
        seq[:, 0] = np.linspace(20, 40, 20)   # temp up
        seq[:, 1] = np.linspace(70, 50, 20)   # humidity down (normal)
        result = PhysicsValidator.validate(seq, self._NOW)
        assert not result.confirmed or result.reason != "psychrometric_impossibility"

    # ── Sprint C: diurnal suppression rule ────────────────────────────────

    def test_diurnal_suppression_fires_for_co_moving_gradual_trends(self) -> None:
        """Rule 5 suppresses normal diurnal variation: temp & hum co-moving,
        gradual per-step change, all values within physical sensor ranges."""
        seq = np.tile([25.0, 65.0, 400.0, 300.0], (20, 1)).astype(float)
        # Gradual rising temp (trend ≈ 0.105/step < 0.5 threshold)
        seq[:, 0] = np.linspace(22.0, 24.0, 20)
        # Gradual rising hum (trend ≈ 0.263/step < 1.0 threshold) — same direction
        seq[:, 1] = np.linspace(55.0, 60.0, 20)
        result = PhysicsValidator.validate(seq, self._NOW)
        assert result.suppressed is True
        assert result.confirmed is False
        assert result.reason == "normal_diurnal_variation"

    def test_diurnal_suppression_not_fired_when_trends_oppose(self) -> None:
        """Rule 5 must not fire when temp rises while humidity falls (not co-moving)."""
        seq = np.tile([25.0, 65.0, 400.0, 300.0], (20, 1)).astype(float)
        seq[:, 0] = np.linspace(22.0, 24.0, 20)   # temp up
        seq[:, 1] = np.linspace(60.0, 55.0, 20)   # hum down
        result = PhysicsValidator.validate(seq, self._NOW)
        assert result.suppressed is False

    def test_diurnal_suppression_not_fired_when_temp_constant(self) -> None:
        """Rule 5 requires temp to be changing; a flat temp disables co-moving check."""
        seq = np.tile([25.0, 65.0, 400.0, 300.0], (20, 1)).astype(float)
        # temp constant → trend = 0, is_temp_changing = False → co-moving = False
        seq[:, 1] = np.linspace(55.0, 60.0, 20)
        result = PhysicsValidator.validate(seq, self._NOW)
        assert result.suppressed is False

    def test_diurnal_suppression_not_fired_when_temp_out_of_greenhouse_range(self) -> None:
        """Rule 5 requires all temp values within TEMP_RANGE; out-of-range disables suppression."""
        seq = np.tile([25.0, 65.0, 400.0, 300.0], (20, 1)).astype(float)
        # Temp gradual but starts below the 20°C floor
        seq[:, 0] = np.linspace(18.0, 22.0, 20)
        seq[:, 1] = np.linspace(55.0, 60.0, 20)
        result = PhysicsValidator.validate(seq, self._NOW)
        assert result.suppressed is False

    def test_diurnal_suppression_has_lower_priority_than_confirmation_rules(self) -> None:
        """Rule 5 must not fire when a confirmation rule (CO2 out-of-range) precedes it."""
        seq = np.tile([25.0, 65.0, 100.0, 300.0], (20, 1)).astype(float)
        # co-moving gradual trends that would normally suppress
        seq[:, 0] = np.linspace(22.0, 24.0, 20)
        seq[:, 1] = np.linspace(55.0, 60.0, 20)
        # CO2 below 200 ppm → Rule 3 fires first (confirmed)
        result = PhysicsValidator.validate(seq, self._NOW)
        assert result.confirmed is True
        assert result.reason == "co2_out_of_range"
        assert result.suppressed is False

    # ── Sprint C: utc_offset_hours parameter ──────────────────────────────

    def test_utc_offset_hours_shifts_local_hour_into_night(self) -> None:
        """A UTC+2 greenhouse at 22:00 UTC is at local midnight (00:00) — night.
        High light should fire nocturnal_light regardless of UTC time."""
        # 20:00 UTC with +2 offset → local 22:00, which is in NIGHT_HOURS
        ts = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)
        seq = _seq(light=500.0)
        result = PhysicsValidator.validate(seq, ts, utc_offset_hours=2)
        assert result.confirmed is True
        assert result.reason == "nocturnal_light"

    def test_utc_offset_hours_shifts_local_hour_out_of_night(self) -> None:
        """UTC 01:00 is night at offset=0 but daytime at offset=-5 (UTC-5 is 20:00 local)."""
        ts = datetime(2026, 6, 20, 1, 0, tzinfo=timezone.utc)  # 01:00 UTC = night
        seq = _seq(light=500.0)
        # Without offset: fires (1 in NIGHT_HOURS)
        result_utc = PhysicsValidator.validate(seq, ts, utc_offset_hours=0)
        assert result_utc.confirmed is True
        # UTC-5 local hour = (1 - 5) % 24 = 20 → NOT in NIGHT_HOURS
        result_offset = PhysicsValidator.validate(seq, ts, utc_offset_hours=-5)
        assert result_offset.reason != "nocturnal_light"

    def test_utc_offset_zero_default_matches_explicit(self) -> None:
        """Default utc_offset_hours=0 produces identical results to passing 0 explicitly."""
        seq = _seq()
        result_default = PhysicsValidator.validate(seq, self._NOW)
        result_explicit = PhysicsValidator.validate(seq, self._NOW, utc_offset_hours=0)
        assert result_default == result_explicit


# ---------------------------------------------------------------------------
# GasAnomalyDetector
# ---------------------------------------------------------------------------

class TestGasAnomalyDetector:
    def _baseline_readings(self, n=30) -> list[dict]:
        rng = np.random.default_rng(42)
        return [
            {
                "c2h4": float(rng.normal(0.05, 0.005)),
                "gas_co2": float(rng.normal(400.0, 10.0)),
                "no": float(rng.normal(1.0, 0.1)),
                "no2": float(rng.normal(5.0, 0.5)),
                "nox": float(rng.normal(6.0, 0.6)),
                "co": float(rng.normal(0.2, 0.02)),
            }
            for _ in range(n)
        ]

    def test_fit_returns_baselines_for_all_columns(self) -> None:
        detector = GasAnomalyDetector()
        baselines = detector.fit(self._baseline_readings())
        assert set(baselines.keys()) == {"c2h4", "gas_co2", "no", "no2", "nox", "co"}

    def test_fit_skips_columns_with_too_few_values(self) -> None:
        detector = GasAnomalyDetector()
        readings = [{"c2h4": 0.05} for _ in range(3)]  # fewer than min samples
        baselines = detector.fit(readings)
        assert "c2h4" not in baselines

    def test_normal_reading_not_anomaly(self) -> None:
        detector = GasAnomalyDetector()
        baselines = detector.fit(self._baseline_readings())
        normal = {"c2h4": 0.05, "gas_co2": 400.0, "no": 1.0, "no2": 5.0, "nox": 6.0, "co": 0.2}
        results = detector.detect(normal, baselines)
        assert not any(r.is_anomaly for r in results.values())

    def test_extreme_value_is_anomaly(self) -> None:
        detector = GasAnomalyDetector()
        baselines = detector.fit(self._baseline_readings())
        extreme = {"gas_co2": 10000.0}  # far beyond baseline
        results = detector.detect(extreme, baselines)
        assert results["gas_co2"].is_anomaly
        assert results["gas_co2"].severity == "critical"

    def test_severity_levels(self) -> None:
        detector = GasAnomalyDetector()
        baselines = detector.fit(self._baseline_readings(n=50))
        bl = baselines["gas_co2"]
        # warning: 2-3 std
        warning_val = bl.mean + 2.5 * bl.std
        res = detector.detect({"gas_co2": warning_val}, baselines)
        assert res["gas_co2"].severity == "warning"

    def test_zero_std_returns_not_anomaly(self) -> None:
        from src.models.gas_anomaly_detector import GasBaseline
        detector = GasAnomalyDetector()
        constant_baseline = {"c2h4": GasBaseline(
            column="c2h4", mean=0.05, std=0.0, q25=0.05, q75=0.05, iqr=0.0, n=10
        )}
        result = detector.detect({"c2h4": 0.05}, constant_baseline)
        assert not result["c2h4"].is_anomaly

    def test_missing_column_in_values_skipped(self) -> None:
        detector = GasAnomalyDetector()
        baselines = detector.fit(self._baseline_readings())
        result = detector.detect({"c2h4": 0.05}, baselines)
        assert "gas_co2" not in result

    # ── Sprint C: _collect_column_values helper coverage ──────────────────

    def test_collect_column_values_skips_non_convertible_string(self) -> None:
        """_collect_column_values must silently skip values that raise ValueError."""
        detector = GasAnomalyDetector()
        # Mix valid readings with a row containing an unparseable string
        readings = [{"c2h4": 0.05} for _ in range(8)]
        readings.append({"c2h4": "not_a_number"})  # triggers ValueError in float()
        col_values = detector._collect_column_values(readings)
        # Only the 8 valid floats should be collected; the string row is discarded
        assert len(col_values["c2h4"]) == 8

    def test_collect_column_values_skips_none_values(self) -> None:
        """_collect_column_values skips None entries (missing sensor field)."""
        detector = GasAnomalyDetector()
        readings = [{"c2h4": 0.05} for _ in range(5)]
        readings += [{"c2h4": None} for _ in range(3)]
        col_values = detector._collect_column_values(readings)
        assert len(col_values["c2h4"]) == 5

    def test_collect_column_values_skips_inf_float(self) -> None:
        """_collect_column_values must discard non-finite floats (inf, nan)."""
        detector = GasAnomalyDetector()
        readings = [{"c2h4": 0.05} for _ in range(6)]
        readings.append({"c2h4": float("inf")})   # not finite
        readings.append({"c2h4": float("nan")})   # not finite
        col_values = detector._collect_column_values(readings)
        assert len(col_values["c2h4"]) == 6

    # ── Sprint C: detect() helper coverage ───────────────────────────────

    def test_detect_skips_non_convertible_string_in_values(self) -> None:
        """detect() must silently skip value entries that can't convert to float."""
        detector = GasAnomalyDetector()
        baselines = detector.fit(self._baseline_readings())
        result = detector.detect({"gas_co2": "not_a_number"}, baselines)
        assert "gas_co2" not in result

    def test_detect_skips_non_finite_value(self) -> None:
        """detect() must discard inf/nan values before scoring."""
        detector = GasAnomalyDetector()
        baselines = detector.fit(self._baseline_readings())
        result_inf = detector.detect({"gas_co2": float("inf")}, baselines)
        result_nan = detector.detect({"gas_co2": float("nan")}, baselines)
        assert "gas_co2" not in result_inf
        assert "gas_co2" not in result_nan

    # ── Sprint C: _score_column helper edge cases ──────────────────────────

    def test_score_column_iqr_only_anomaly_uses_iqr_method(self) -> None:
        """When z-score < critical but value is outside IQR fences, method == 'iqr'."""
        from src.models.gas_anomaly_detector import GasBaseline
        detector = GasAnomalyDetector()
        # Baseline: mean=10, std=1 (z-score for value=13 is 3 — just at critical).
        # Use a value that has z<3 but is outside the IQR fence.
        # IQR fence: q25 - 1.5*iqr = 9 - 1.5*2 = 6; q75 + 1.5*iqr = 11 + 3 = 14
        # z-score for 15: |15-10|/1 = 5 > 3 → z_anomaly
        # Use value=13 where z=3 (not > 3): z_anomaly=False but iqr_anomaly may depend on fence
        # Better: craft so z < 3 but outside IQR fence
        # mean=100, std=50 → z=2 for value=200; IQR is tiny → fences near [95,105]
        bl = GasBaseline(column="gas_co2", mean=100.0, std=50.0,
                         q25=99.0, q75=101.0, iqr=2.0, n=100)
        # value=200: z = |200-100|/50 = 2 < 3 (not z_anomaly)
        # IQR fence upper = 101 + 1.5*2 = 104 → 200 > 104 → iqr_anomaly
        result = detector._score_column(200.0, bl)
        assert result.is_anomaly is True
        assert result.method == "iqr"

    def test_score_column_iqr_zero_does_not_produce_iqr_anomaly(self) -> None:
        """When IQR == 0, the IQR fence branch is skipped and iqr_anomaly stays False."""
        from src.models.gas_anomaly_detector import GasBaseline
        detector = GasAnomalyDetector()
        # std > 0 but iqr = 0 (identical quartiles, slight variance at extremes)
        bl = GasBaseline(column="gas_co2", mean=400.0, std=5.0,
                         q25=400.0, q75=400.0, iqr=0.0, n=20)
        # value near mean: z < 2, no z anomaly, no iqr anomaly
        result = detector._score_column(401.0, bl)
        assert result.is_anomaly is False
        assert result.method == "none"

    def test_build_baseline_computes_correct_statistics(self) -> None:
        """_build_baseline helper returns a GasBaseline with correct statistics."""
        detector = GasAnomalyDetector()
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        bl = detector._build_baseline("gas_co2", vals)
        assert bl.column == "gas_co2"
        assert bl.n == 10
        assert bl.mean == pytest.approx(5.5)
        assert bl.iqr == pytest.approx(bl.q75 - bl.q25)
        assert bl.iqr > 0

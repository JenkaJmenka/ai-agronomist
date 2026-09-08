"""Unit tests for _fault_type_from_anomaly_type — real-detector coverage.

Verifies that every anomaly_type string written by the production detectors
(SetpointAwareScadaDetector and BandBreachDetector) is correctly mapped to
a fault_type key from comprehensive_pipeline._RULE_VOTE_PROFILES, and that
the two previously-invented synthetic entries ('random', 'multi_sensor') now
return None instead of unjustified fault keys.
"""

from __future__ import annotations

import pytest

from src.api.services.anomaly_group_service import _fault_type_from_anomaly_type
from src.models.band_breach_detector import BAND_TYPE_TO_VOTE
from src.models.setpoint_scada_detector import (
    DEHUMIDIFICATION_FAILURE,
    HUMIDITY_SATURATION_RISK,
    OVER_HEATING,
    SENSOR_FLATLINE,
    THERMAL_DIVERGENCE,
    UNDER_HEATING,
)


# ─── SCADA detector types ────────────────────────────────────────────────────


class TestScadaDetectorMapping:
    """UNDER_HEATING, OVER_HEATING, etc. written by SetpointAwareScadaDetector."""

    @pytest.mark.parametrize(
        "anomaly_type, expected_fault",
        [
            (UNDER_HEATING, "under_heating"),
            (OVER_HEATING, "over_heating"),
            (DEHUMIDIFICATION_FAILURE, "dehumidification_failure"),
            (HUMIDITY_SATURATION_RISK, "humidity_saturation_risk"),
            (THERMAL_DIVERGENCE, "thermal_divergence"),
            # SENSOR_FLATLINE maps to stuck_fault — a zero-variance frozen
            # sensor is a stuck sensor by the db/models.py taxonomy.
            (SENSOR_FLATLINE, "stuck_fault"),
        ],
    )
    def test_scada_type_maps_to_correct_fault(
        self, anomaly_type: str, expected_fault: str
    ) -> None:
        result = _fault_type_from_anomaly_type(anomaly_type)
        assert result == expected_fault, (
            f"Expected {anomaly_type!r} → {expected_fault!r}, got {result!r}"
        )

    def test_sensor_flatline_maps_to_stuck_not_its_own_key(self) -> None:
        """SENSOR_FLATLINE must map to 'stuck_fault', not 'sensor_flatline'.

        The db/models.py taxonomy has no 'sensor_flatline' key; the correct
        category is 'stuck_fault'.  Verify we didn't accidentally return the
        raw anomaly_type string.
        """
        result = _fault_type_from_anomaly_type(SENSOR_FLATLINE)
        assert result != "sensor_flatline"
        assert result == "stuck_fault"


# ─── Band-breach detector types ──────────────────────────────────────────────


class TestBandBreachDetectorMapping:
    """TEMP_BAND_HIGH, VPD_BAND_LOW, etc. written by BandBreachDetector."""

    @pytest.mark.parametrize(
        "anomaly_type, expected_fault",
        [
            ("TEMP_BAND_HIGH", "temp_band_high"),
            ("TEMP_BAND_LOW", "temp_band_low"),
            ("VPD_BAND_HIGH", "vpd_band_high"),
            ("VPD_BAND_LOW", "vpd_band_low"),
            ("HUMIDITY_BAND_HIGH", "humidity_band_high"),
            ("HUMIDITY_BAND_LOW", "humidity_band_low"),
            ("CO2_BAND_HIGH", "co2_band_high"),
            ("CO2_BAND_LOW", "co2_band_low"),
        ],
    )
    def test_band_type_maps_to_correct_fault(
        self, anomaly_type: str, expected_fault: str
    ) -> None:
        result = _fault_type_from_anomaly_type(anomaly_type)
        assert result == expected_fault, (
            f"Expected {anomaly_type!r} → {expected_fault!r}, got {result!r}"
        )

    def test_band_type_to_vote_fully_covered(self) -> None:
        """Every key in BAND_TYPE_TO_VOTE must be covered by the mapping."""
        for anomaly_type, expected_fault in BAND_TYPE_TO_VOTE.items():
            result = _fault_type_from_anomaly_type(anomaly_type)
            assert result == expected_fault, (
                f"BAND_TYPE_TO_VOTE key {anomaly_type!r} → {expected_fault!r}, "
                f"but mapping returned {result!r}"
            )


# ─── Synthetic types — GAP 3 fixes ───────────────────────────────────────────


class TestSyntheticTypeMapping:
    """Verify the two previously-invented entries now return None."""

    def test_random_returns_none(self) -> None:
        """'random' perturbs all four sensors — not attributable to spike_fault."""
        assert _fault_type_from_anomaly_type("random") is None

    def test_multi_sensor_returns_none(self) -> None:
        """'multi_sensor' is explicitly multivariate — not accuracy_decline."""
        assert _fault_type_from_anomaly_type("multi_sensor") is None

    def test_synthetic_context_returns_none(self) -> None:
        """sentinel for normal background readings must never yield a fault."""
        assert _fault_type_from_anomaly_type("synthetic_context") is None

    @pytest.mark.parametrize(
        "anomaly_type, expected_fault",
        [
            ("temperature_spike", "spike_fault"),
            ("temperature_drop", "spike_fault"),
            ("humidity_spike", "spike_fault"),
            ("humidity_drop", "spike_fault"),
            ("co2_surge", "spike_fault"),
            ("light_failure", "spike_fault"),
            ("gradual_drift", "calibration_drift"),
        ],
    )
    def test_deterministic_synthetic_types_still_map(
        self, anomaly_type: str, expected_fault: str
    ) -> None:
        """The remaining 7 synthetic types must still resolve to their fault key."""
        result = _fault_type_from_anomaly_type(anomaly_type)
        assert result == expected_fault


# ─── Edge cases ──────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_none_input_returns_none(self) -> None:
        assert _fault_type_from_anomaly_type(None) is None

    def test_unrecognised_string_returns_none(self) -> None:
        assert _fault_type_from_anomaly_type("not_a_real_type") is None

    def test_lowercase_scada_type_not_mapped(self) -> None:
        """SCADA constants are uppercase; lowercase must not match."""
        assert _fault_type_from_anomaly_type("under_heating") is None

    def test_empty_string_returns_none(self) -> None:
        assert _fault_type_from_anomaly_type("") is None

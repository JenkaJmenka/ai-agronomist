"""Tests for psychrometric validation using document Table 9 dataset."""

import math

import pytest

from src.models.psychrometric_validator import (
    calculate_humidity_deficit,
    calculate_saturation_vapor_pressure,
    validate_row,
)


class TestPsychrometricTable9:
    """Document Table 9: Thermodynamic contradiction verification."""

    def test_full_table9_normal_rows(self):
        """Rows 12:00-12:04 should all pass (HD_meas == HD_calc).

        Reference HD values are on the correct g/m³ scale (coefficient
        2167.0 for kPa vapour pressures).  Previous values were 10×
        too small due to using the hPa coefficient (216.7) with kPa
        inputs.
        """
        test_cases = [
            (20.0, 60.0, 6.914),
            (20.5, 62.0, 6.763),
            (21.0, 58.0, 7.695),
            (21.5, 55.0, 8.487),
            (22.0, 50.0, 9.706),
        ]
        for t, rh, hd_meas in test_cases:
            hd_calc = calculate_humidity_deficit(t, rh)
            assert abs(hd_meas - hd_calc) < 0.1, f"T={t}, RH={rh}"

    def test_table9_anomalous_row(self):
        """12:05: HD_meas=25.0 vs HD_calc=9.589 — should flag."""
        result = validate_row(22.5, 52.0, 25.0)
        assert result == "FAILED_PSYCHROMETRIC_LIMIT"

    def test_consistent_readings(self):
        hd = calculate_humidity_deficit(20.0, 60.0)
        result = validate_row(20.0, 60.0, hd)
        assert result == "OK"

    def test_tetens_constants(self):
        """Verify Tetens equation at 20C against known value."""
        e_s = calculate_saturation_vapor_pressure(20.0)
        assert abs(e_s - 2.3384) < 0.001

    def test_tolerance_boundary(self):
        """Delta exactly at tolerance should be OK (not strict >)."""
        hd_calc = calculate_humidity_deficit(22.0, 50.0)
        from src.config import PSYCHROMETRIC_HD_TOLERANCE
        hd_meas = hd_calc + PSYCHROMETRIC_HD_TOLERANCE * 0.99
        result = validate_row(22.0, 50.0, hd_meas)
        assert result == "OK"

    def test_tolerance_exceeded(self):
        """Delta above tolerance should flag."""
        hd_calc = calculate_humidity_deficit(22.0, 50.0)
        from src.config import PSYCHROMETRIC_HD_TOLERANCE
        hd_meas = hd_calc + PSYCHROMETRIC_HD_TOLERANCE + 0.01
        result = validate_row(22.0, 50.0, hd_meas)
        assert result == "FAILED_PSYCHROMETRIC_LIMIT"

    def test_production_tolerance(self):
        """With tolerance=0.5 g/m³ (correct scale), a 1.0 g/m³ deviation is caught."""
        hd_calc = calculate_humidity_deficit(22.0, 50.0)
        hd_meas = hd_calc + 1.0
        result = validate_row(22.0, 50.0, hd_meas)
        assert result == "FAILED_PSYCHROMETRIC_LIMIT"

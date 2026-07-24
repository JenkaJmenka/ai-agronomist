"""Golden-value tests for the corrected psychrometric humidity deficit.

Verifies that the 216.7 -> 2167.0 coefficient fix produces physically
correct HD values (g/m3) when using kPa vapour pressures end-to-end.
"""

import math

import pytest

from src.models.psychrometric_validator import (
    calculate_humidity_deficit,
    calculate_saturation_vapor_pressure,
)


class TestGoldenHDValues:
    """Reference HD values computed from first principles.

    HD = 2167 * (e_s - e_a) / T_K, where e_s and e_a are in kPa.
    """

    def test_25c_50pct_rh(self) -> None:
        """25 C / 50% RH -> approximately 11.5 g/m3."""
        hd = calculate_humidity_deficit(25.0, 50.0)
        assert abs(hd - 11.5) < 0.1, f"Expected ~11.5 g/m3, got {hd:.4f}"

    def test_20c_70pct_rh(self) -> None:
        """20 C / 70% RH -> approximately 5.2 g/m3."""
        hd = calculate_humidity_deficit(20.0, 70.0)
        assert abs(hd - 5.2) < 0.1, f"Expected ~5.2 g/m3, got {hd:.4f}"

    def test_manual_computation_25c_50pct(self) -> None:
        """Verify against hand-computed values step by step."""
        t_air = 25.0
        rh = 50.0
        e_s = calculate_saturation_vapor_pressure(t_air)
        e_a = e_s * (rh / 100.0)
        t_kelvin = t_air + 273.15
        expected = 2167.0 * (e_s - e_a) / t_kelvin
        hd = calculate_humidity_deficit(t_air, rh)
        assert abs(hd - expected) < 1e-9, (
            f"HD mismatch: got {hd:.6f}, expected {expected:.6f}"
        )

    def test_manual_computation_20c_70pct(self) -> None:
        """Verify against hand-computed values step by step."""
        t_air = 20.0
        rh = 70.0
        e_s = calculate_saturation_vapor_pressure(t_air)
        e_a = e_s * (rh / 100.0)
        t_kelvin = t_air + 273.15
        expected = 2167.0 * (e_s - e_a) / t_kelvin
        hd = calculate_humidity_deficit(t_air, rh)
        assert abs(hd - expected) < 1e-9, (
            f"HD mismatch: got {hd:.6f}, expected {expected:.6f}"
        )

    def test_rh_100_gives_zero(self) -> None:
        """100% RH -> zero deficit (saturated air)."""
        hd = calculate_humidity_deficit(25.0, 100.0)
        assert abs(hd) < 1e-9

    def test_rh_0_gives_maximum(self) -> None:
        """0% RH -> maximum possible HD for that temperature."""
        hd = calculate_humidity_deficit(25.0, 0.0)
        # At 25 C, e_s ~ 3.167 kPa, e_a = 0.
        # HD = 2167 * 3.167 / 298.15 ~ 23.0 g/m3
        assert 22.0 < hd < 24.0, f"Expected ~23 g/m3, got {hd:.4f}"

    def test_physically_reasonable_range(self) -> None:
        """Typical greenhouse conditions produce HD in ~2-15 g/m3."""
        for t, rh in [(20.0, 80.0), (25.0, 70.0), (30.0, 60.0)]:
            hd = calculate_humidity_deficit(t, rh)
            assert 2.0 < hd < 16.0, (
                f"HD out of physical range at T={t}, RH={rh}: {hd:.4f}"
            )

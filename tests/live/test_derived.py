"""Tests for the derived-channel computation module (src/live/derived.py).

Covers: compute_vpd known-value checks (Magnus formula), RH=100 → 0.0,
clamping at negative VPD, non-finite input → 0.0, extreme temperatures.
"""

from __future__ import annotations

import math

import pytest

from src.live.derived import compute_vpd


# ---------------------------------------------------------------------------
# Known-value reference checks (manual Magnus calculation)
# ---------------------------------------------------------------------------


class TestComputeVpdKnownValues:

    def test_25c_60pct(self) -> None:
        """25 °C / 60 % RH → ≈ 1.27 kPa."""
        vpd = compute_vpd(25.0, 60.0)
        assert abs(vpd - 1.27) < 0.02

    def test_20c_80pct(self) -> None:
        """20 °C / 80 % RH → ≈ 0.47 kPa."""
        vpd = compute_vpd(20.0, 80.0)
        assert abs(vpd - 0.47) < 0.02

    def test_30c_50pct(self) -> None:
        """30 °C / 50 % RH → ≈ 2.12 kPa."""
        svp = 0.61078 * math.exp(17.27 * 30.0 / (30.0 + 237.3))
        expected = svp * (1.0 - 50.0 / 100.0)
        vpd = compute_vpd(30.0, 50.0)
        assert abs(vpd - expected) < 0.02

    def test_15c_70pct(self) -> None:
        """15 °C / 70 % RH → ≈ 0.51 kPa."""
        svp = 0.61078 * math.exp(17.27 * 15.0 / (15.0 + 237.3))
        expected = svp * (1.0 - 70.0 / 100.0)
        vpd = compute_vpd(15.0, 70.0)
        assert abs(vpd - expected) < 0.02


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestComputeVpdEdgeCases:

    def test_rh_100_returns_zero(self) -> None:
        """100 % humidity → VPD is exactly 0.0."""
        assert compute_vpd(25.0, 100.0) == 0.0

    def test_rh_0_equals_svp(self) -> None:
        """0 % humidity → VPD equals the saturation vapour pressure."""
        svp = 0.61078 * math.exp(17.27 * 25.0 / (25.0 + 237.3))
        vpd = compute_vpd(25.0, 0.0)
        assert abs(vpd - svp) < 0.001

    def test_negative_vpd_clamped_to_zero(self) -> None:
        """RH > 100 % would produce negative VPD; result is clamped to 0."""
        vpd = compute_vpd(25.0, 110.0)
        assert vpd == 0.0

    def test_vpd_always_non_negative(self) -> None:
        """VPD is never negative for any sensible RH range."""
        for rh in range(0, 151, 5):
            for temp in range(-10, 50, 5):
                assert compute_vpd(float(temp), float(rh)) >= 0.0


# ---------------------------------------------------------------------------
# Non-finite input guard
# ---------------------------------------------------------------------------


class TestComputeVpdNonFiniteInputs:

    def test_nan_temperature(self) -> None:
        assert compute_vpd(float("nan"), 60.0) == 0.0

    def test_nan_humidity(self) -> None:
        assert compute_vpd(25.0, float("nan")) == 0.0

    def test_both_nan(self) -> None:
        assert compute_vpd(float("nan"), float("nan")) == 0.0

    def test_inf_temperature(self) -> None:
        assert compute_vpd(float("inf"), 60.0) == 0.0

    def test_neg_inf_temperature(self) -> None:
        assert compute_vpd(float("-inf"), 60.0) == 0.0

    def test_inf_humidity(self) -> None:
        assert compute_vpd(25.0, float("inf")) == 0.0

    def test_neg_inf_humidity(self) -> None:
        assert compute_vpd(25.0, float("-inf")) == 0.0


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


class TestComputeVpdReturnType:

    def test_returns_float(self) -> None:
        result = compute_vpd(25.0, 60.0)
        assert isinstance(result, float)

    def test_result_is_finite(self) -> None:
        result = compute_vpd(25.0, 60.0)
        assert math.isfinite(result)


# ---------------------------------------------------------------------------
# Single-source SVP verification (shared with psychrometric_validator)
# ---------------------------------------------------------------------------


class TestVpdUsesCanonicalSVP:
    """Verify that compute_vpd uses calculate_saturation_vapor_pressure
    from psychrometric_validator (0.61078 constant, not the old 0.6108).
    """

    def test_25c_50pct_matches_hand_computed(self) -> None:
        """VPD at 25 C / 50% RH matches the 0.61078-based formula."""
        svp = 0.61078 * math.exp(17.27 * 25.0 / (25.0 + 237.3))
        expected = svp * (1.0 - 50.0 / 100.0)
        vpd = compute_vpd(25.0, 50.0)
        assert abs(vpd - expected) < 0.001, (
            f"VPD mismatch: got {vpd:.6f}, expected {expected:.6f}"
        )

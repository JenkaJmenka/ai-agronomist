"""Tests for the psychrometric validator (Tetens equation).

Covers:
- Saturation vapour pressure at a known reference temperature (25°C).
- Humidity deficit at RH=100 (zero) and RH=50 (positive).
- validate_row OK and FAILED_PSYCHROMETRIC_LIMIT paths.
- ValueError for unphysical inputs (RH > 100, T ≤ absolute zero).
"""

import math

import pytest

from src.config import PSYCHROMETRIC_HD_TOLERANCE
from src.models.psychrometric_validator import (
    calculate_humidity_deficit,
    calculate_saturation_vapor_pressure,
    validate_row,
)

# ---------------------------------------------------------------------------
# calculate_saturation_vapor_pressure
# ---------------------------------------------------------------------------


def test_saturation_vapor_pressure_at_known_values() -> None:
    """Tetens equation at T=25°C must return ~3.167 kPa (reference value)."""
    e_s = calculate_saturation_vapor_pressure(25.0)
    # Published reference: 3.1677 kPa (Tetens 1930, used by WMO)
    assert abs(e_s - 3.1677) < 0.001, (
        f"Expected e_s ≈ 3.167 kPa at 25°C, got {e_s:.4f}"
    )


def test_saturation_vapor_pressure_increases_with_temperature() -> None:
    """e_s must be strictly monotonically increasing with temperature."""
    temps = [0.0, 10.0, 20.0, 30.0, 40.0]
    pressures = [calculate_saturation_vapor_pressure(t) for t in temps]
    for i in range(len(pressures) - 1):
        assert pressures[i] < pressures[i + 1], (
            f"e_s not monotone: {pressures[i]:.4f} ≥ {pressures[i + 1]:.4f} "
            f"at T={temps[i]}°C → T={temps[i + 1]}°C"
        )


def test_saturation_vapor_pressure_raises_below_absolute_zero() -> None:
    """Temperature at or below -273.15°C must raise ValueError."""
    with pytest.raises(ValueError, match="absolute zero"):
        calculate_saturation_vapor_pressure(-273.16)


def test_saturation_vapor_pressure_raises_at_exact_absolute_zero() -> None:
    """Exactly -273.15°C (absolute zero) must raise ValueError."""
    with pytest.raises(ValueError, match="absolute zero"):
        calculate_saturation_vapor_pressure(-273.15)


# ---------------------------------------------------------------------------
# calculate_humidity_deficit
# ---------------------------------------------------------------------------


def test_humidity_deficit_zero_at_rh100() -> None:
    """HD must be zero (or negligible) when RH = 100% — air is saturated."""
    hd = calculate_humidity_deficit(25.0, 100.0)
    assert abs(hd) < 1e-9, f"Expected HD ≈ 0 at RH=100%, got {hd}"


def test_humidity_deficit_positive_at_rh50() -> None:
    """HD must be strictly positive when RH < 100%."""
    hd = calculate_humidity_deficit(25.0, 50.0)
    assert hd > 0.0, f"Expected HD > 0 at RH=50%, got {hd}"


def test_humidity_deficit_decreases_as_rh_increases() -> None:
    """Higher RH (same T) means less deficit — HD must decrease as RH rises."""
    hd_low = calculate_humidity_deficit(25.0, 40.0)
    hd_high = calculate_humidity_deficit(25.0, 80.0)
    assert hd_low > hd_high, (
        f"Expected HD(RH=40%) > HD(RH=80%), got {hd_low:.4f} vs {hd_high:.4f}"
    )


def test_humidity_deficit_raises_on_negative_rh() -> None:
    """RH < 0 is unphysical and must raise ValueError."""
    with pytest.raises(ValueError):
        calculate_humidity_deficit(25.0, -1.0)


def test_humidity_deficit_raises_on_rh_above_100() -> None:
    """RH > 100 is unphysical and must raise ValueError."""
    with pytest.raises(ValueError):
        calculate_humidity_deficit(25.0, 101.0)


def test_humidity_deficit_raises_on_absolute_zero_temp() -> None:
    """T ≤ -273.15°C is unphysical and must raise ValueError."""
    with pytest.raises(ValueError):
        calculate_humidity_deficit(-274.0, 60.0)


# ---------------------------------------------------------------------------
# validate_row
# ---------------------------------------------------------------------------


def test_validate_row_ok_when_within_tolerance() -> None:
    """Row with hd_measured == hd_calc (zero deviation) must return 'OK'."""
    hd_calc = calculate_humidity_deficit(25.0, 60.0)
    status = validate_row(25.0, 60.0, hd_measured=hd_calc)
    assert status == "OK"


def test_validate_row_ok_at_tolerance_boundary() -> None:
    """Row with |deviation| clearly inside tolerance must return 'OK'."""
    hd_calc = calculate_humidity_deficit(25.0, 60.0)
    # Use a deviation that is strictly less than PSYCHROMETRIC_HD_TOLERANCE (0.05)
    # to avoid floating-point boundary issues.
    just_inside = PSYCHROMETRIC_HD_TOLERANCE * 0.5  # 0.025 g/m³ — well inside
    status = validate_row(25.0, 60.0, hd_measured=hd_calc + just_inside)
    assert status == "OK"


def test_validate_row_failed_when_outside_tolerance() -> None:
    """Row with |HD_measured - HD_calc| > tolerance must return 'FAILED_PSYCHROMETRIC_LIMIT'."""
    hd_calc = calculate_humidity_deficit(25.0, 60.0)
    hd_measured = hd_calc + 1.0  # 1.0 g/m³ >> PSYCHROMETRIC_HD_TOLERANCE (0.05)
    status = validate_row(25.0, 60.0, hd_measured=hd_measured)
    assert status == "FAILED_PSYCHROMETRIC_LIMIT"


def test_validate_row_failed_when_hd_measured_is_too_low() -> None:
    """Negative deviation beyond tolerance must also return 'FAILED_PSYCHROMETRIC_LIMIT'."""
    hd_calc = calculate_humidity_deficit(25.0, 60.0)
    hd_measured = hd_calc - 1.0
    status = validate_row(25.0, 60.0, hd_measured=hd_measured)
    assert status == "FAILED_PSYCHROMETRIC_LIMIT"


def test_validate_row_raises_on_unphysical_rh() -> None:
    """RH = 150 is unphysical — validate_row must raise ValueError."""
    with pytest.raises(ValueError, match="outside the valid range"):
        validate_row(25.0, 150.0, hd_measured=1.0)


def test_validate_row_raises_on_absolute_zero_temp() -> None:
    """T = -274°C is below absolute zero — validate_row must raise ValueError."""
    with pytest.raises(ValueError, match="absolute zero"):
        validate_row(-274.0, 60.0, hd_measured=1.0)


def test_validate_row_ok_at_rh_boundaries() -> None:
    """RH=0 and RH=100 are both valid boundary inputs and must not raise."""
    hd_at_rh0 = calculate_humidity_deficit(25.0, 0.0)
    hd_at_rh100 = calculate_humidity_deficit(25.0, 100.0)

    status_0 = validate_row(25.0, 0.0, hd_measured=hd_at_rh0)
    status_100 = validate_row(25.0, 100.0, hd_measured=hd_at_rh100)

    assert status_0 == "OK"
    assert status_100 == "OK"

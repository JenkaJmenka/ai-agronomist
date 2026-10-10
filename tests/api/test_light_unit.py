"""Acceptance tests for light-channel unit reconciliation (Task 4).

These tests verify:
  1. The native clear-sky July fixture produces ZERO light alerts.
  2. W/m² and lux columns carrying the same illumination normalise to the same value.
  3. A genuinely anomalous light value still alerts (regression guard).
  4. The alert-text unit suffix and dashboard label both match the canonical unit.
  5. A light column with no unit marker is handled by the documented default and
     that defaulting is recorded.

All tests must FAIL before the Task-4 unit reconciliation and PASS after.
"""
from __future__ import annotations

import io
import textwrap

import pandas as pd
import pytest

from src.config import (
    LIGHT_CANONICAL_UNIT,
    LIGHT_RANGE,
    OOB_LIMITS,
    SENSOR_UNITS,
)
from src.api.services.anomaly_group_service import _SIMPLE_REASONING_UNITS
from src.dashboard.components.charts import SENSOR_LABELS
from src.vendors.csv_adapter import (
    CSVAdapter,
    LightInputUnit,
    LIGHT_CANONICAL_UNIT as ADAPTER_LIGHT_CANONICAL_UNIT,
    LUX_TO_W_PER_M2,
    PPFD_UMOL_TO_W_PER_M2,
    infer_light_unit,
    parse_raw_rows,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURE_PATH = "tests/fixtures/native_july_clearsky.csv"


def _normalize_csv_string(csv_text: str) -> pd.DataFrame:
    """Run the CSVAdapter normalize path on an in-memory CSV string."""
    from unittest.mock import patch, MagicMock

    buf = io.StringIO(csv_text)
    df_raw = pd.read_csv(buf, sep=",")
    adapter = CSVAdapter({})
    # normalize() expects a DataFrame; bypass connect/read.
    return adapter.normalize(df_raw)


def _parse_raw_csv_string(csv_text: str) -> tuple[pd.DataFrame, list[str]]:
    """Run parse_raw_rows on an in-memory CSV string."""
    buf = io.StringIO(csv_text)
    chunk = pd.read_csv(buf, sep=",")
    return parse_raw_rows(chunk)


# ---------------------------------------------------------------------------
# Test 1 — native clear-sky July fixture produces ZERO light alerts
# ---------------------------------------------------------------------------


def test_clearsky_fixture_all_light_values_within_operational_band() -> None:
    """Every light reading in native_july_clearsky.csv fits inside LIGHT_RANGE.

    Before the fix LIGHT_RANGE was (0.0, 1000.0) and the fixture maximum is
    1019.98 W/m², so 1019.98 > 1000.0 tripped the in-band alert gate.
    After the fix LIGHT_RANGE is (0.0, 1200.0) W/m² and all values fit.
    """
    adapter = CSVAdapter({"filepath": _FIXTURE_PATH})
    adapter.connect()
    raw = adapter.read()
    df = adapter.normalize(raw)

    lo, hi = LIGHT_RANGE
    violations = df.loc[(df["light"] < lo) | (df["light"] > hi), "light"]
    assert violations.empty, (
        f"LIGHT_RANGE {LIGHT_RANGE} W/m² violated by {len(violations)} rows; "
        f"max observed = {df['light'].max():.2f} W/m²"
    )


def test_clearsky_fixture_all_light_values_within_oob_limits() -> None:
    """Every light reading in the fixture is within the hard OOB limits.

    The OOB limit was previously 200000.0 (lux-scaled) and is now 2000.0 W/m².
    All fixture values (max 1019.98 W/m²) must be within [0.0, 2000.0].
    """
    adapter = CSVAdapter({"filepath": _FIXTURE_PATH})
    adapter.connect()
    raw = adapter.read()
    df = adapter.normalize(raw)

    oob_lo, oob_hi = OOB_LIMITS["light"]
    violations = df.loc[(df["light"] < oob_lo) | (df["light"] > oob_hi), "light"]
    assert violations.empty, (
        f"OOB_LIMITS {OOB_LIMITS['light']} W/m² violated by {len(violations)} rows; "
        f"max observed = {df['light'].max():.2f} W/m²"
    )


def test_clearsky_fixture_maximum_light_value_correct() -> None:
    """Fixture maximum light value after normalization is ~1019.98 W/m².

    Verifies that the ingest path does NOT convert (divide) the W/m² source
    values — they should pass through unchanged, not be shrunk.
    """
    adapter = CSVAdapter({"filepath": _FIXTURE_PATH})
    adapter.connect()
    raw = adapter.read()
    df = adapter.normalize(raw)

    max_light = df["light"].max()
    assert max_light == pytest.approx(1019.98, rel=0.01), (
        f"Expected fixture max ~1019.98 W/m², got {max_light:.4f}"
    )


def test_clearsky_fixture_light_source_unit_recorded_as_w_per_m2() -> None:
    """The fixture's light column carries '(W/m²)' → unit recorded as W_PER_M2."""
    adapter = CSVAdapter({"filepath": _FIXTURE_PATH})
    adapter.connect()
    raw = adapter.read()
    df = adapter.normalize(raw)

    unit = df.attrs.get("light_source_unit")
    assert unit == LightInputUnit.W_PER_M2, (
        f"Expected LightInputUnit.W_PER_M2, got {unit!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — W/m² and lux columns with the same physical illumination match
# ---------------------------------------------------------------------------


def test_w_per_m2_and_lux_same_illumination_normalise_to_same_value_normalize_path() -> None:
    """CSVAdapter.normalize(): W/m² and lux inputs for equal illumination → equal output.

    Physical illumination: 5.0 W/m² irradiance ≡ 600.0 lux (600 × 1/120 = 5.0).
    """
    wm2_csv = textwrap.dedent("""\
        timestamp,temperature,humidity,Light Intensity (W/m²)
        2026-01-01 00:00,22.0,65.0,5.0
    """)
    lux_csv = textwrap.dedent("""\
        timestamp,temperature,humidity,Light (lux)
        2026-01-01 00:00,22.0,65.0,600.0
    """)

    df_wm2 = _normalize_csv_string(wm2_csv)
    df_lux = _normalize_csv_string(lux_csv)

    assert "light" in df_wm2.columns, "W/m² CSV did not produce a 'light' column"
    assert "light" in df_lux.columns, "lux CSV did not produce a 'light' column"

    wm2_val = float(df_wm2["light"].iloc[0])
    lux_val = float(df_lux["light"].iloc[0])
    assert wm2_val == pytest.approx(lux_val, rel=1e-4), (
        f"W/m² input gave {wm2_val:.6f} W/m² but lux input gave {lux_val:.6f} W/m² "
        "(should be equal after lux→W/m² conversion)"
    )


def test_w_per_m2_and_lux_same_illumination_normalise_to_same_value_raw_path() -> None:
    """parse_raw_rows(): W/m² and lux inputs for equal illumination → equal output."""
    wm2_csv = textwrap.dedent("""\
        timestamp,temperature,humidity,Light Intensity (W/m²)
        2026-01-01 00:00,22.0,65.0,5.0
    """)
    lux_csv = textwrap.dedent("""\
        timestamp,temperature,humidity,Light (lux)
        2026-01-01 00:00,22.0,65.0,600.0
    """)

    df_wm2, _ = _parse_raw_csv_string(wm2_csv)
    df_lux, _ = _parse_raw_csv_string(lux_csv)

    assert "light" in df_wm2.columns
    assert "light" in df_lux.columns

    wm2_val = float(df_wm2["light"].iloc[0])
    lux_val = float(df_lux["light"].iloc[0])
    assert wm2_val == pytest.approx(lux_val, rel=1e-4), (
        f"parse_raw_rows: W/m² gave {wm2_val:.6f} but lux gave {lux_val:.6f}"
    )


# ---------------------------------------------------------------------------
# Test 3 — genuinely anomalous light values still alert (regression guard)
# ---------------------------------------------------------------------------


def test_anomalously_high_light_exceeds_oob_limit() -> None:
    """A light value above any conceivable solar irradiance exceeds OOB_LIMITS.

    3000 W/m² is about 2.5× the maximum solar irradiance at Earth's surface
    and would only occur from a hardware fault (direct lamp on sensor, short).
    It must remain above OOB_LIMITS after the unit fix so alerts are not disabled.
    """
    oob_lo, oob_hi = OOB_LIMITS["light"]
    anomalous_value = 3000.0  # W/m²
    assert anomalous_value > oob_hi, (
        f"Anomalous value {anomalous_value} W/m² should exceed OOB upper bound "
        f"{oob_hi} W/m² — do not widen OOB_LIMITS to suppress this"
    )


def test_anomalously_low_light_exceeds_operational_band() -> None:
    """A plausibly valid but out-of-band low value still sits outside LIGHT_RANGE.

    This test uses -5.0 W/m² (impossible — irradiance cannot be negative).
    It must remain outside LIGHT_RANGE lower bound (0.0).
    """
    lo, _ = LIGHT_RANGE
    negative_irradiance = -5.0
    assert negative_irradiance < lo, (
        f"Negative irradiance {negative_irradiance} should be below LIGHT_RANGE lower "
        f"bound {lo} W/m²"
    )


def test_fixture_max_within_operational_band_but_anomalous_5x_still_oob() -> None:
    """The fixture's max (1019.98) fits in the band; 5× that (5100) exceeds OOB."""
    fixture_max = 1019.98
    lo, hi = LIGHT_RANGE
    oob_lo, oob_hi = OOB_LIMITS["light"]

    assert lo <= fixture_max <= hi, (
        f"Fixture max {fixture_max} should be inside LIGHT_RANGE {LIGHT_RANGE}"
    )
    assert fixture_max * 5 > oob_hi, (
        f"5× fixture max ({fixture_max * 5:.1f}) should exceed OOB upper bound {oob_hi}"
    )


# ---------------------------------------------------------------------------
# Test 4 — alert text suffix and dashboard label match the canonical unit
# ---------------------------------------------------------------------------


def test_reasoning_unit_suffix_matches_canonical_unit() -> None:
    """_SIMPLE_REASONING_UNITS['light'] must match the canonical unit string."""
    expected_suffix = f" {LIGHT_CANONICAL_UNIT}"
    actual_suffix = _SIMPLE_REASONING_UNITS.get("light", "")
    assert actual_suffix == expected_suffix, (
        f"Alert unit suffix is {actual_suffix!r}, expected {expected_suffix!r}"
    )


def test_sensor_labels_light_matches_canonical_unit() -> None:
    """Dashboard SENSOR_LABELS['light'] must include the canonical unit string."""
    label = SENSOR_LABELS.get("light", "")
    assert LIGHT_CANONICAL_UNIT in label, (
        f"SENSOR_LABELS['light'] = {label!r} does not contain canonical unit "
        f"{LIGHT_CANONICAL_UNIT!r}"
    )


def test_config_sensor_units_light_matches_canonical_unit() -> None:
    """SENSOR_UNITS['light'] in config.py must match the canonical unit string."""
    unit = SENSOR_UNITS.get("light", "")
    assert LIGHT_CANONICAL_UNIT in unit, (
        f"SENSOR_UNITS['light'] = {unit!r} does not contain canonical unit "
        f"{LIGHT_CANONICAL_UNIT!r}"
    )


def test_adapter_canonical_unit_agrees_with_config() -> None:
    """LIGHT_CANONICAL_UNIT in csv_adapter and config.py must be identical."""
    assert ADAPTER_LIGHT_CANONICAL_UNIT == LIGHT_CANONICAL_UNIT, (
        f"Adapter canonical unit {ADAPTER_LIGHT_CANONICAL_UNIT!r} differs from "
        f"config canonical unit {LIGHT_CANONICAL_UNIT!r}"
    )


# ---------------------------------------------------------------------------
# Test 5 — no-unit headers are handled by documented default and recorded
# ---------------------------------------------------------------------------


def test_no_unit_header_inferred_as_defaulted() -> None:
    """A bare 'Light' header with no unit marker returns DEFAULTED_W_PER_M2."""
    result = infer_light_unit("Light")
    assert result == LightInputUnit.DEFAULTED_W_PER_M2, (
        f"Expected DEFAULTED_W_PER_M2 for header 'Light', got {result!r}"
    )


def test_no_unit_header_variant_sensor_light_defaulted() -> None:
    """'light sensor' with no unit marker also returns DEFAULTED_W_PER_M2."""
    result = infer_light_unit("light sensor reading")
    assert result == LightInputUnit.DEFAULTED_W_PER_M2


def test_no_unit_header_recorded_in_df_attrs_normalize_path() -> None:
    """A no-unit light column → DEFAULTED_W_PER_M2 recorded in df.attrs."""
    csv_text = textwrap.dedent("""\
        timestamp,temperature,humidity,light
        2026-01-01 00:00,22.0,65.0,300.0
    """)
    df = _normalize_csv_string(csv_text)
    unit = df.attrs.get("light_source_unit")
    assert unit == LightInputUnit.DEFAULTED_W_PER_M2, (
        f"Expected DEFAULTED_W_PER_M2 in df.attrs, got {unit!r}"
    )


def test_no_unit_header_recorded_in_df_attrs_raw_path() -> None:
    """parse_raw_rows: a no-unit light column → DEFAULTED_W_PER_M2 in df.attrs."""
    csv_text = textwrap.dedent("""\
        timestamp,temperature,humidity,light
        2026-01-01 00:00,22.0,65.0,300.0
    """)
    df, _ = _parse_raw_csv_string(csv_text)
    unit = df.attrs.get("light_source_unit")
    assert unit == LightInputUnit.DEFAULTED_W_PER_M2, (
        f"Expected DEFAULTED_W_PER_M2 in df.attrs, got {unit!r}"
    )


def test_no_unit_header_value_unchanged_normalize_path() -> None:
    """Defaulted W/m² header: value passes through unchanged (no division)."""
    csv_text = textwrap.dedent("""\
        timestamp,temperature,humidity,light
        2026-01-01 00:00,22.0,65.0,300.0
    """)
    df = _normalize_csv_string(csv_text)
    assert float(df["light"].iloc[0]) == pytest.approx(300.0), (
        "Default-unit light value should not be converted (should remain 300.0 W/m²)"
    )


# ---------------------------------------------------------------------------
# Additional unit-inference tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("header,expected", [
    ("Light Intensity (W/m²) [connext2:METEO LIGHT SENSOR]", LightInputUnit.W_PER_M2),
    ("Light Intensity (W/m2)", LightInputUnit.W_PER_M2),
    ("light w/m", LightInputUnit.W_PER_M2),
    ("irradiance sensor", LightInputUnit.W_PER_M2),
    ("Light (lux)", LightInputUnit.LUX),
    ("illuminance reading", LightInputUnit.LUX),
    ("PPFD sensor", LightInputUnit.PPFD_UMOL),
    ("PAR µmol/m²/s", LightInputUnit.PPFD_UMOL),
    ("light intensity", LightInputUnit.DEFAULTED_W_PER_M2),
    ("Light", LightInputUnit.DEFAULTED_W_PER_M2),
    ("light reading", LightInputUnit.DEFAULTED_W_PER_M2),
])
def test_infer_light_unit_parametrized(
    header: str, expected: LightInputUnit
) -> None:
    """infer_light_unit classifies known headers correctly."""
    result = infer_light_unit(header)
    assert result == expected, (
        f"Header {header!r}: expected {expected}, got {result}"
    )


def test_lux_to_w_per_m2_conversion_constant() -> None:
    """LUX_TO_W_PER_M2 gives the expected approximate factor."""
    # 120 lux ≈ 1 W/m² for standard daylight
    assert 120.0 * LUX_TO_W_PER_M2 == pytest.approx(1.0, rel=1e-6)


def test_ppfd_to_w_per_m2_conversion_constant() -> None:
    """PPFD_UMOL_TO_W_PER_M2 gives the expected approximate factor."""
    # 4.57 µmol·m⁻²·s⁻¹ ≈ 1 W/m² for PAR 400-700 nm
    assert 4.57 * PPFD_UMOL_TO_W_PER_M2 == pytest.approx(1.0, rel=1e-4)

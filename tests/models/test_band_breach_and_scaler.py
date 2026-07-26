"""Unit tests for BandBreachDetector day/night crop bands."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from src.models.band_breach_detector import (
    CO2_BAND_HIGH,
    CO2_BAND_LOW,
    HUMIDITY_BAND_HIGH,
    TEMP_BAND_HIGH,
    BandBreachDetector,
)
from src.models.operational_config_store import default_operational_config


def test_tomato_day_temp_breach() -> None:
    config = default_operational_config("tomato")
    # Daytime (12:00 UTC) — tomato day max is 26 °C.
    ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    window = np.tile(np.array([[28.0, 70.0, 500.0, 400.0]]), (5, 1))
    findings = BandBreachDetector().detect(window, config, timestamp=ts)
    types = {f.anomaly_type for f in findings}
    assert TEMP_BAND_HIGH in types


def test_no_breach_inside_band() -> None:
    config = default_operational_config("tomato")
    ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    # co2=800.0 is inside the tomato day CO2 band [600, 1100] ppm; no finding expected.
    window = np.tile(np.array([[22.0, 70.0, 800.0, 400.0]]), (5, 1))
    findings = BandBreachDetector().detect(window, config, timestamp=ts)
    assert findings == []


# ---------------------------------------------------------------------------
# CO2 enrichment flag tests
# ---------------------------------------------------------------------------


def test_unenriched_house_at_420ppm_no_co2_band_low() -> None:
    """Default config (co2_enrichment=False) must NOT emit CO2_BAND_LOW.

    Ambient outdoor CO2 (~420 ppm) sits permanently below tomato's lower bound
    (600 ppm). Without enrichment equipment the signal is never actionable.
    """
    config = default_operational_config("tomato")
    assert config.get("co2_enrichment") is False, "default must be False"

    ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    # 420 ppm < 600 ppm band min; all other channels are normal.
    window = np.tile(np.array([[22.0, 70.0, 420.0, 400.0]]), (5, 1))
    findings = BandBreachDetector().detect(window, config, timestamp=ts)
    types = {f.anomaly_type for f in findings}
    assert CO2_BAND_LOW not in types, (
        "CO2_BAND_LOW must be suppressed for an unenriched greenhouse"
    )


def test_unenriched_house_at_1500ppm_co2_band_high_fires() -> None:
    """CO2_BAND_HIGH must still be emitted even when co2_enrichment is False.

    Excess CO2 buildup from plant respiration is always actionable — it needs no
    enrichment equipment to diagnose.
    """
    config = default_operational_config("tomato")
    ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    # 1500 ppm > 1100 ppm band max.
    window = np.tile(np.array([[22.0, 70.0, 1500.0, 400.0]]), (5, 1))
    findings = BandBreachDetector().detect(window, config, timestamp=ts)
    types = {f.anomaly_type for f in findings}
    assert CO2_BAND_HIGH in types, "CO2_BAND_HIGH must fire regardless of enrichment flag"
    assert CO2_BAND_LOW not in types


def test_enriched_house_at_420ppm_co2_band_low_fires() -> None:
    """When co2_enrichment=True the CO2_BAND_LOW signal is enabled.

    A grower with enrichment equipment expects the system to maintain the lower
    bound; dropping to ambient is a genuine fault ("enrichment underperforming").
    """
    config = default_operational_config("tomato")
    config["co2_enrichment"] = True

    ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    window = np.tile(np.array([[22.0, 70.0, 420.0, 400.0]]), (5, 1))
    findings = BandBreachDetector().detect(window, config, timestamp=ts)
    types = {f.anomaly_type for f in findings}
    assert CO2_BAND_LOW in types, "CO2_BAND_LOW must fire when co2_enrichment is True"


def test_other_channels_unaffected_by_co2_enrichment_flag() -> None:
    """Temperature and humidity band detections are unaffected by co2_enrichment."""
    config = default_operational_config("tomato")
    ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    # Humidity at 95% > tomato day max 80% → HUMIDITY_BAND_HIGH should fire
    # with the default unenriched config.
    window = np.tile(np.array([[22.0, 95.0, 420.0, 400.0]]), (5, 1))
    findings = BandBreachDetector().detect(window, config, timestamp=ts)
    types = {f.anomaly_type for f in findings}
    assert HUMIDITY_BAND_HIGH in types, (
        "HUMIDITY_BAND_HIGH must fire independently of co2_enrichment"
    )
    assert CO2_BAND_LOW not in types, "CO2_BAND_LOW still suppressed for unenriched config"


def test_co2_enrichment_missing_key_treated_as_false() -> None:
    """A config dict that predates the co2_enrichment key is handled safely.

    Missing key → treat as False (conservative: suppresses CO2_BAND_LOW so old
    configs gain alarm-fatigue protection without any migration).
    """
    config = default_operational_config("tomato")
    del config["co2_enrichment"]  # simulate a pre-flag config document

    ts = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    window = np.tile(np.array([[22.0, 70.0, 420.0, 400.0]]), (5, 1))
    findings = BandBreachDetector().detect(window, config, timestamp=ts)
    types = {f.anomaly_type for f in findings}
    assert CO2_BAND_LOW not in types, (
        "Missing co2_enrichment key must default to False (suppress CO2_BAND_LOW)"
    )


def test_seasonal_scaler_clamp_and_override() -> None:
    from src.models.seasonal_scaler import build_scaler_from_baselines

    baselines = {
        "global": {"temperature": {"std": 2.0}},
        "seasons": {
            "summer": {
                "temperature": {"mean": 24.0, "std": 6.0, "median": 24.0, "n": 1000},
            },
            "winter": {
                "temperature": {"mean": 18.0, "std": 1.0, "median": 18.0, "n": 1000},
            },
        },
    }
    scaler = build_scaler_from_baselines(
        baselines,
        overrides={"winter": {"detector_scale": 1.25}},
    )
    # 6/2 = 3.0 → clamped to 2.0
    assert scaler.factor("summer", "temperature") == 2.0
    assert scaler.factor("winter", "temperature") == 1.25
    # Sparse season falls back to 1.0
    sparse = build_scaler_from_baselines(
        {
            "global": {"temperature": {"std": 2.0}},
            "seasons": {
                "summer": {
                    "temperature": {"mean": 24.0, "std": 6.0, "median": 24.0, "n": 10},
                },
            },
        }
    )
    assert sparse.factor("summer", "temperature") == 1.0

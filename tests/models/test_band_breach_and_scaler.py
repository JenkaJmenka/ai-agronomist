"""Unit tests for BandBreachDetector day/night crop bands."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from src.models.band_breach_detector import (
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
    window = np.tile(np.array([[22.0, 70.0, 500.0, 400.0]]), (5, 1))
    findings = BandBreachDetector().detect(window, config, timestamp=ts)
    assert findings == []


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

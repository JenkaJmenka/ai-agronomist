"""Unit tests for Pass-1 MAD robust Z scrubbing."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.models.mad_scrub import compute_mad_flags, mad_z_scores


def test_mad_z_scores_zero_when_mad_is_zero() -> None:
    z = mad_z_scores(np.array([5.0, 5.0, 5.0, 5.0]))
    assert np.allclose(z, 0.0)


def test_mad_z_scores_flags_extreme_outlier() -> None:
    # Need non-zero MAD: majority-constant series collapses MAD to 0.
    rng = np.random.default_rng(0)
    values = 22.0 + rng.normal(0, 0.3, size=100)
    values[-1] = 80.0
    z = mad_z_scores(values)
    assert abs(z[-1]) > 3.5


def test_compute_mad_flags_marks_outlier_row() -> None:
    rng = np.random.default_rng(1)
    n = 100
    df = pd.DataFrame(
        {
            "temperature": 22.0 + rng.normal(0, 0.4, size=n),
            "humidity": 60.0 + rng.normal(0, 1.0, size=n),
            "co2": 400.0 + rng.normal(0, 5.0, size=n),
            "light": 300.0 + rng.normal(0, 10.0, size=n),
        }
    )
    df.loc[df.index[-1], "temperature"] = 55.0
    flags = compute_mad_flags(df, ["temperature", "humidity", "co2", "light"])
    assert bool(flags.iloc[-1]) is True
    assert int(flags.sum()) >= 1

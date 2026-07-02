"""
Tests verifying equivalence of numpy-based sliding window variance against pandas-based implementation.
"""

import numpy as np
import pandas as pd
import pytest

from src.config import SENSOR_NAMES
from src.models.stuck_fault_detector import StuckFaultDetector


def pandas_stuck_detect(detector: StuckFaultDetector, sequence: np.ndarray, skip_channels=None) -> dict[str, bool]:
    _SENSOR_NAMES = list(SENSOR_NAMES)
    skip = skip_channels or set()
    num_sensors = min(sequence.shape[1], len(_SENSOR_NAMES))
    results = {}
    for idx in range(num_sensors):
        name = _SENSOR_NAMES[idx]
        if name in skip:
            results[name] = False
            continue
        channel = sequence[:, idx]
        value_range = float(np.max(channel) - np.min(channel))
        if sequence.shape[0] >= detector._window_k:
            variance = float(
                pd.Series(channel)
                .rolling(window=detector._window_k, min_periods=detector._window_k)
                .var()
                .min()
            )
        else:
            variance = float(np.var(channel))
        if np.isnan(variance):
            variance = 1.0
        is_flat = value_range < detector._flat_range_threshold
        results[name] = bool(
            is_flat and variance < detector._variance_threshold
        )
    return results


@pytest.fixture
def detector() -> StuckFaultDetector:
    return StuckFaultDetector(variance_threshold=1e-4, window_k=10, flat_range_threshold=0.1)


def test_equivalence_with_pandas_implementation(detector: StuckFaultDetector):
    rng = np.random.default_rng(42)

    # 1. Normal noisy sequence
    normal_seq = rng.normal(loc=[25.0, 65.0, 400.0, 500.0], scale=[0.5, 1.0, 5.0, 10.0], size=(30, 4))

    # 2. Sequence with stuck channels
    stuck_seq = normal_seq.copy()
    stuck_seq[:, 0] = 25.0  # completely stuck temperature
    stuck_seq[10:20, 1] = 65.0  # partially stuck humidity (but overall range is still normal)

    # 3. Short sequence (shorter than window_k)
    short_seq = rng.normal(loc=[25.0, 65.0, 400.0, 500.0], scale=[0.5, 1.0, 5.0, 10.0], size=(5, 4))

    # 4. Short sequence stuck
    short_stuck_seq = np.ones((5, 4)) * np.array([25.0, 65.0, 400.0, 500.0])

    # 5. Sequence with NaN values
    nan_seq = normal_seq.copy()
    nan_seq[:, 2] = np.nan  # All NaNs

    test_cases = [
        ("normal", normal_seq),
        ("stuck", stuck_seq),
        ("short", short_seq),
        ("short_stuck", short_stuck_seq),
        ("nan", nan_seq),
    ]

    for name, seq in test_cases:
        res_pandas = pandas_stuck_detect(detector, seq)
        res_numpy = detector.detect(seq)
        assert res_numpy == res_pandas, f"Mismatch on test case: {name}"

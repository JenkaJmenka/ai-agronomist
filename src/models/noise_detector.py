"""
Precision degradation / noise fault detector (SCADA spec section 1.4).

Algorithm:
    1. Pass signal through 2nd-order high-pass Butterworth filter.
    2. Compute rolling standard deviation of filtered signal.
    3. Alarm if rolling_std > SENSOR_NOISE_TARGETS[sensor] * NOISE_ALPHA.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt

try:
    from src.config import NOISE_ALPHA, SENSOR_NOISE_TARGETS
except ImportError:  # pragma: no cover
    NOISE_ALPHA: float = 3.0  # type: ignore[no-redef]
    SENSOR_NOISE_TARGETS: dict[str, float] = {  # type: ignore[no-redef]
        "temperature": 0.3,
        "humidity": 1.0,
        "co2": 10.0,
        "light": 30.0,
    }


class NoiseDetector:
    """Detects precision degradation via high-pass filtered rolling std."""

    # Normalized cutoff frequency for the Butterworth HP filter.
    # 0.1 = 10% of Nyquist (half-cycles / sample, range [0, 1]).
    HP_CUTOFF: float = 0.1

    def detect(
        self,
        series: np.ndarray,
        sensor: str,
    ) -> bool:
        """Return True if high-frequency noise exceeds σ_target * NOISE_ALPHA.

        Args:
            series: 1-D float array of sensor readings (length = seq_len).
            sensor: Sensor name key into SENSOR_NOISE_TARGETS.

        Returns:
            True if precision degradation detected.
        """
        # filtfilt default padlen = 3 * max(len(a), len(b)) - 1 = 8 for N=2;
        # input must be > padlen, so minimum 10 samples for safety.
        if len(series) < 10:
            return False
        target = SENSOR_NOISE_TARGETS.get(sensor)
        if target is None:
            return False
        # Wn is normalized frequency (half-cycles/sample): 0 < Wn < 1.
        b, a = butter(N=2, Wn=self.HP_CUTOFF, btype="high")
        filtered = filtfilt(b, a, series.astype(float))
        filtered_std = float(pd.Series(filtered).std())
        return filtered_std > target * NOISE_ALPHA

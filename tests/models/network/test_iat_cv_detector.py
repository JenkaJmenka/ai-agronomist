"""Tests for IATCVDetector using document Table 12 dataset."""

import numpy as np
import pytest

from src.models.network.iat_cv_detector import IATCVDetector


class TestIATCVDetector:
    """Document Table 12: Inter-packet interval variation (w=5, cv=1.5, spacing=3.0)."""

    def test_full_table12_dataset(self):
        """Replay Table 12 core scenarios.

        The document's row at 12:05 (IAT=4.95s) triggers the spacing
        rule (4.95 > 3*1.0).  The CV rule triggers when high-variance
        windows accumulate enough spread to cross the CV threshold.
        """
        iat = np.array([1.0, 1.0, 1.0, 1.0, 1.0, 4.95, 0.05, 0.01])
        detector = IATCVDetector(
            window=5, cv_threshold=1.5, baseline_mean=1.0, spacing_multiplier=3.0,
        )
        flags = detector.detect(iat)

        assert flags[0:4] == [False, False, False, False], "Insufficient window"
        assert flags[4] is False, "Stable polling CV=0"
        assert flags[5] is True, "Spacing 4.95 > 3.0"

    def test_stable_polling(self):
        iat = np.array([1.0] * 10)
        detector = IATCVDetector(window=5, cv_threshold=1.5, baseline_mean=1.0, spacing_multiplier=3.0)
        flags = detector.detect(iat)
        assert not any(flags)

    def test_spacing_violation(self):
        iat = np.array([1.0, 1.0, 1.0, 1.0, 5.0])
        detector = IATCVDetector(window=5, cv_threshold=1.5, baseline_mean=1.0, spacing_multiplier=3.0)
        flags = detector.detect(iat)
        assert flags[4] is True

    def test_cv_violation(self):
        """Construct a window with CV > 1.5 (high variance, low mean)."""
        iat = np.array([0.01, 0.01, 5.0, 0.01, 0.01])
        detector = IATCVDetector(window=5, cv_threshold=1.5, baseline_mean=1.0, spacing_multiplier=100.0)
        flags = detector.detect(iat)
        assert flags[4] is True

    def test_short_series(self):
        detector = IATCVDetector(window=5, cv_threshold=1.5, baseline_mean=1.0, spacing_multiplier=3.0)
        flags = detector.detect(np.array([1.0, 1.0]))
        assert not any(flags)

    def test_invalid_window_raises(self):
        with pytest.raises(ValueError):
            IATCVDetector(window=1)

    def test_production_params(self):
        """w=20, cv=1.0, spacing=2.5 — tighter thresholds."""
        iat = np.array([1.0] * 19 + [3.0])
        detector = IATCVDetector(window=20, cv_threshold=1.0, baseline_mean=1.0, spacing_multiplier=2.5)
        flags = detector.detect(iat)
        assert flags[19] is True

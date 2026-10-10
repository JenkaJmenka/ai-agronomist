"""Task 2c item 5: gas statistical thresholds live in src/config.py.

``_Z_WARNING``/``_Z_CRITICAL`` moved out of ``gas_anomaly_detector.py``
(previously module-local constants) into ``src.config`` as
``GAS_Z_WARNING``/``GAS_Z_CRITICAL``, with identical values -- both
``gas_anomaly_detector.py`` and its importer ``cold_run_detector.py`` now
read the same config constants rather than each holding (or re-exporting)
its own copy. Detection-count invariance on fixture A at every chunk size
is covered separately by
``tests/workers/test_stat_chunk_invariance.py`` (pins 102 total statistical
hits unchanged); this file pins the values themselves.
"""

from __future__ import annotations

from src.config import GAS_Z_CRITICAL, GAS_Z_WARNING
from src.models import cold_run_detector, gas_anomaly_detector


class TestGasThresholdsLiveInConfigUnchanged:
    def test_values_are_unchanged(self) -> None:
        assert GAS_Z_WARNING == 2.0
        assert GAS_Z_CRITICAL == 3.0

    def test_warning_is_strictly_below_critical(self) -> None:
        """Non-vacuity: a misconfigured pair would silently invert severity."""
        assert GAS_Z_WARNING < GAS_Z_CRITICAL


class TestBothConsumersReadTheSameConfigValues:
    """Neither module holds its own copy any more -- both alias the same
    src.config constants, so there is exactly one place to change them.
    """

    def test_gas_anomaly_detector_reads_the_config_values(self) -> None:
        assert gas_anomaly_detector._Z_WARNING == GAS_Z_WARNING
        assert gas_anomaly_detector._Z_CRITICAL == GAS_Z_CRITICAL

    def test_cold_run_detector_reads_the_same_config_values(self) -> None:
        assert cold_run_detector._Z_WARNING == GAS_Z_WARNING
        assert cold_run_detector._Z_CRITICAL == GAS_Z_CRITICAL

import io
import math
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.models.cold_run_detector import (
    ColdRunStatisticalDetector,
    RollingAnomaly,
    _effective_rolling_window,
)


def detect_rolling_reference(
    detector: ColdRunStatisticalDetector,
    readings: list[dict[str, Any]],
    window: int,
) -> list[RollingAnomaly]:
    """Reference implementation matching the original loop-based detect_rolling."""
    effective_window = _effective_rolling_window(len(readings), window)
    if effective_window <= 0 or len(readings) <= effective_window:
        return []

    columns = detector._discover_columns(readings)
    anomalies: list[RollingAnomaly] = []
    min_window_points = max(5, effective_window // 4)

    for col in columns:
        values, timestamps = detector._column_series(readings, col)
        streak = 0
        for idx in range(effective_window, len(values)):
            current = values[idx]
            if not math.isfinite(current):
                streak = 0
                continue
            history = values[idx - effective_window : idx]
            history = history[np.isfinite(history)]
            if history.size < min_window_points:
                streak = 0
                continue
            std = float(np.std(history))
            if std < 1e-9:
                streak = 0
                continue
            z_score = abs(current - float(np.mean(history))) / std
            if z_score < detector._z_min:
                streak = 0
                continue
            streak += 1
            if streak < detector._min_consecutive:
                continue
            if z_score >= 3.0:  # _Z_CRITICAL
                severity = "critical"
            elif z_score >= 2.0:  # _Z_WARNING
                severity = "warning"
            else:
                severity = "normal"
            anomalies.append(
                RollingAnomaly(
                    timestamp=timestamps[idx],
                    column=col,
                    value=float(current),
                    z_score=z_score,
                    severity=severity,
                )
            )
    return anomalies


def test_detect_rolling_equivalence():
    """Verify that vectorized detect_rolling behaves identically to the reference loop-based one."""
    # Test Scenario 1: standard dataset with normal values and a spike
    readings = [
        {"timestamp": f"2026-06-09T11:00:{i:02d}Z", "outdoor_temp": 15.0 + (i % 5) * 0.1}
        for i in range(40)
    ]
    # Add a streak of spike values
    readings.extend(
        {"timestamp": f"2026-06-09T11:01:{i:02d}Z", "outdoor_temp": 35.0}
        for i in range(5)
    )

    detector = ColdRunStatisticalDetector(z_min=3.0, min_consecutive=2)
    
    ref_anomalies = detect_rolling_reference(detector, readings, window=12)
    vec_anomalies = detector.detect_rolling(readings, window=12)

    assert len(ref_anomalies) == len(vec_anomalies)
    for ref, vec in zip(ref_anomalies, vec_anomalies):
        assert ref.timestamp == vec.timestamp
        assert ref.column == vec.column
        assert ref.value == vec.value
        assert pytest.approx(ref.z_score, abs=1e-7) == vec.z_score
        assert ref.severity == vec.severity


def test_detect_rolling_nan_handling():
    """Verify identical handling of NaNs, None and non-finite values."""
    readings = [
        {"timestamp": f"2026-06-09T11:00:{i:02d}Z", "outdoor_temp": 15.0 + (i % 5) * 0.1}
        for i in range(40)
    ]
    # Add None, NaN, inf, and verify they reset streaks
    readings.append({"timestamp": "2026-06-09T11:01:00Z", "outdoor_temp": None})
    readings.append({"timestamp": "2026-06-09T11:01:01Z", "outdoor_temp": float("nan")})
    readings.append({"timestamp": "2026-06-09T11:01:02Z", "outdoor_temp": float("inf")})
    # Add a spike that would have triggered if the streak wasn't reset
    readings.extend(
        {"timestamp": f"2026-06-09T11:01:{i:02d}Z", "outdoor_temp": 40.0}
        for i in range(5)
    )

    detector = ColdRunStatisticalDetector(z_min=3.0, min_consecutive=3)
    ref_anomalies = detect_rolling_reference(detector, readings, window=12)
    vec_anomalies = detector.detect_rolling(readings, window=12)

    assert len(ref_anomalies) == len(vec_anomalies)
    for ref, vec in zip(ref_anomalies, vec_anomalies):
        assert ref.timestamp == vec.timestamp
        assert ref.column == vec.column
        assert ref.value == vec.value
        assert pytest.approx(ref.z_score, abs=1e-7) == vec.z_score
        assert ref.severity == vec.severity


def test_detect_rolling_streaks_and_severities():
    """Verify different min_consecutive thresholds and severity levels."""
    # std of history is 0, wait, if std is 0, we skip.
    # So let's add some noise to make std > 0
    readings = [
        {"timestamp": f"2026-06-09T11:00:{i:02d}Z", "outdoor_temp": 10.0 + (i % 2) * 1.0}
        for i in range(30)
    ]
    # History has mean=10.5, std=0.5.
    # Z-score of 10.5 + 2.5 * 0.5 = 11.75 is 2.5 (warning)
    # Z-score of 10.5 + 3.5 * 0.5 = 12.25 is 3.5 (critical)
    readings.append({"timestamp": "2026-06-09T11:01:00Z", "outdoor_temp": 11.75}) # warning
    readings.append({"timestamp": "2026-06-09T11:01:01Z", "outdoor_temp": 12.25}) # critical

    detector = ColdRunStatisticalDetector(z_min=2.0, min_consecutive=1)
    ref_anomalies = detect_rolling_reference(detector, readings, window=12)
    vec_anomalies = detector.detect_rolling(readings, window=12)

    assert len(ref_anomalies) == len(vec_anomalies)
    for ref, vec in zip(ref_anomalies, vec_anomalies):
        assert ref.severity == vec.severity
        assert pytest.approx(ref.z_score, abs=1e-7) == vec.z_score


def test_single_pass_io_mocking():
    """Mock _read_chunks in src/api/services/cold_run_upload.py to verify single-pass I/O."""
    from src.api.services.cold_run_upload import run_cold_run_upload, _read_chunks
    from src.vendors.csv_adapter import CSVAdapter
    from unittest.mock import MagicMock

    # Prepare a simple dataset
    csv_content = (
        "timestamp,temperature,humidity,co2,light\n"
        + "\n".join(
            [f"2026-06-09T11:{i//60:02d}:{i%60:02d}Z,{20+i*0.1},{60+i*0.2},400,500" for i in range(50)]
        )
    )

    # Use a dummy tmp_path and mock pd.read_csv to return the chunks
    adapter = MagicMock(spec=CSVAdapter)
    adapter._csv_props = {"sep": ",", "encoding": "utf-8", "decimal": "."}
    
    # Mock normalize and validate
    df = pd.read_csv(io.StringIO(csv_content))
    adapter.normalize.return_value = df
    adapter.validate.return_value = None

    detector = MagicMock()
    # Mock predict_batch_enriched to return mock values
    detector.predict_batch_enriched.return_value = (
        np.zeros(31, dtype=np.float32),
        np.zeros((31, 16), dtype=np.float32),
        np.zeros(31, dtype=np.float32),
    )

    # Spy on _read_chunks
    call_count = 0
    original_read_chunks = _read_chunks

    def spy_read_chunks(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_read_chunks(*args, **kwargs)

    # Write the csv data to a real temp file so read_csv can read it
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        tmp.write(csv_content)
        tmp_name = tmp.name

    try:
        with patch("src.api.services.cold_run_upload._read_chunks", side_effect=spy_read_chunks):
            response, seq_ts = run_cold_run_upload(
                tmp_path=tmp_name,
                adapter=adapter,
                detector=detector,
                tenant_id="tenant_test",
                greenhouse_id="gh_test",
                seq_len=20,
                chunk_size=1000,
            )
            # Verify that _read_chunks was called exactly once!
            assert call_count == 1
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

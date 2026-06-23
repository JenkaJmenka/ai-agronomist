import pytest
import pandas as pd
import numpy as np

from src.models.postprocessing import (
    map_timestamp_to_anomaly,
    clip_and_scale_anomalies,
    get_high_anomaly_timestamps,
)

def test_map_timestamp_to_anomaly():
    timestamps = pd.Series(["2026-06-09T10:00:00Z", "2026-06-09T10:05:00Z", "2026-06-09T10:10:00Z"])
    reconstruction_error = np.array([0.1, 0.9, 0.4])
    threshold = 0.5
    
    df = map_timestamp_to_anomaly(timestamps, reconstruction_error, threshold)
    assert len(df) == 1
    assert df.iloc[0]["timestamp"] == "2026-06-09T10:05:00Z"
    assert df.iloc[0]["reconstruction_error"] == 0.9

def test_clip_and_scale_anomalies():
    df = pd.DataFrame({
        "timestamp": ["t1", "t2", "t3"],
        "reconstruction_error": [-0.5, 0.4, 1.2]
    })
    scaling_factor = 2.0
    
    scaled_df = clip_and_scale_anomalies(df, scaling_factor)
    assert list(scaled_df["clipped_error"]) == [0.0, 0.4, 1.2]
    assert list(scaled_df["scaled_error"]) == [0.0, 0.8, 2.4]

def test_get_high_anomaly_timestamps():
    df = pd.DataFrame({
        "timestamp": ["t1", "t2", "t3"],
        "reconstruction_error": [0.1, 0.8, 0.9]
    })
    threshold = 0.85
    
    results = get_high_anomaly_timestamps(df, threshold)
    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["timestamp"] == "t3"
    assert results[0]["reconstruction_error"] == 0.9

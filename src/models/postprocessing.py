
import numpy as np
import pandas as pd
from typing import Dict, Any, List

def map_timestamp_to_anomaly(
    timestamps: pd.Series, reconstruction_error: np.ndarray, threshold: float
) -> pd.DataFrame:
    """Maps timestamps to detected anomalies based on reconstruction error."""
    anomalies = pd.DataFrame(
        {"timestamp": timestamps, "reconstruction_error": reconstruction_error.flatten()}
    )
    return anomalies[anomalies["reconstruction_error"] > threshold]

def clip_and_scale_anomalies(
    anomalies: pd.DataFrame, scaling_factor: float
) -> pd.DataFrame:
    """Clips anomaly scores and scales them for visualization."""
    anomalies["clipped_error"] = np.clip(anomalies["reconstruction_error"], 0, None)
    anomalies["scaled_error"] = anomalies["clipped_error"] * scaling_factor
    return anomalies

def get_high_anomaly_timestamps(
    anomalies: pd.DataFrame, threshold: float
) -> List[Dict[str, Any]]:
    """Filters for high-severity anomalies and returns a list of dictionaries."""
    high_anomalies = anomalies[anomalies["reconstruction_error"] > threshold]
    return high_anomalies.to_dict("records")

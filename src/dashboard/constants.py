"""Dashboard-wide constants.

Import these instead of using magic numbers directly in page or component code.
All threshold and timing values trace back to src.config where applicable.
"""
import os

from src.config import ANOMALY_THRESHOLD

DEFAULT_ANOMALY_THRESHOLD: float = ANOMALY_THRESHOLD

CACHE_TTL_SHORT: int = 60    # seconds — lists that change frequently (incidents, greenhouses)
CACHE_TTL_MEDIUM: int = 120  # seconds — sensor readings, anomaly windows
CACHE_TTL_LONG: int = 300    # seconds — timestamp bounds, static upload frames

# Max distinct entries retained per @st.cache_data function (LRU-evicted beyond
# this). Without a bound, every new (tenant, greenhouse, window) combination
# retains its DataFrame until TTL expiry, so panning the timeline accumulates
# many large frames in RAM — a slow, steady dashboard memory climb. Bound the
# entry count so only the few most-recent windows are held.
CACHE_MAX_ENTRIES_FRAME: int = 8   # caches returning full sensor/score DataFrames
CACHE_MAX_ENTRIES_LIST: int = 32   # caches returning small lists/scalars (incidents, bounds)

FAULT_TYPES: list[str] = [
    "spike_fault",
    "calibration_drift",
    "stuck_fault",
    "bias_offset",
    "accuracy_decline",
]

FAULT_LABELS: dict[str, str] = {
    "spike_fault": "Spike fault",
    "calibration_drift": "Calibration drift",
    "stuck_fault": "Stuck sensor",
    "bias_offset": "Bias offset",
    "accuracy_decline": "Accuracy decline",
    "unknown": "Unknown / unclassified",
}

LAYER1_CACHE_DIR: str = os.path.join(
    os.environ.get("DASHBOARD_DATA_DIR", "data"), "inference_result_cache"
)

# st.navigation / st.switch_page paths (relative to app.py).
MONITORING_PAGE: str = "views/1_Active_Monitoring/1_Monitoring.py"
ANOMALIES_PAGE: str = "views/2_anomalies.py"
OPERATOR_FEEDBACK_PAGE: str = "views/99_Operator_Feedback.py"

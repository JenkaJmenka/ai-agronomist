"""
Build operator review queue from open incidents and detected sensor readings.

Sensor readings with high ``anomaly_score`` live in the dashboard DB; the
feedback API only sees **incidents** created by comprehensive inference.
This module bridges the two.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from src.config import INPUT_DIM, SEQ_LEN
from src.dashboard.components.charts import apply_date_filter, query_sensor_readings
from src.dashboard.services.api_client import DashboardApiClient
from src.dashboard.services.incident_loader import fetch_incidents_in_range

SENSOR_VALUE_COLS = ["temperature", "humidity", "co2", "light"]


def _default_row() -> List[float]:
    return [25.0, 65.0, 400.0, 500.0]


def build_sequence_from_readings(
    readings_df: pd.DataFrame,
    anchor_timestamp: pd.Timestamp,
    seq_len: int = SEQ_LEN,
) -> List[List[float]]:
    """Build a ``(seq_len, 4)`` sequence ending at ``anchor_timestamp``."""
    if readings_df.empty:
        return [_default_row() for _ in range(seq_len)]

    work = readings_df.sort_values("timestamp").copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"])
    anchor = pd.to_datetime(anchor_timestamp)
    window = work[work["timestamp"] <= anchor].tail(seq_len)

    rows: List[List[float]] = []
    for _, row in window.iterrows():
        rows.append([float(row[c]) for c in SENSOR_VALUE_COLS])

    if not rows:
        rows = [_default_row()]

    while len(rows) < seq_len:
        rows.insert(0, list(rows[0]))

    if len(rows) > seq_len:
        rows = rows[-seq_len:]

    return rows


def fetch_detected_anomalies(
    tenant_id: str,
    greenhouse_id: str,
    threshold: float,
    start_date=None,
    end_date=None,
    limit: int = 20,
) -> pd.DataFrame:
    """Return sensor readings at or above ``threshold``, newest first."""
    df = query_sensor_readings(tenant_id, greenhouse_id)
    if df.empty or "anomaly_score" not in df.columns:
        return pd.DataFrame()

    df = apply_date_filter(df, start_date, end_date, show_warning=False)
    flagged = df[df["anomaly_score"].fillna(0) >= threshold].copy()
    if flagged.empty:
        return flagged

    flagged["timestamp"] = pd.to_datetime(flagged["timestamp"])
    return flagged.sort_values("timestamp", ascending=False).head(limit)


def _has_nearby_incident(
    timestamp: pd.Timestamp,
    incidents: List[Dict[str, Any]],
    window_minutes: int = 15,
) -> bool:
    ts = pd.to_datetime(timestamp)
    for inc in incidents:
        detected = inc.get("detected_at")
        if detected is None:
            continue
        delta = abs(ts - pd.to_datetime(detected))
        if delta <= pd.Timedelta(minutes=window_minutes):
            return True
    return False


def list_pending_anomalies(
    tenant_id: str,
    greenhouse_id: str,
    threshold: float,
    start_date=None,
    end_date=None,
    existing_incidents: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Detected readings not yet linked to an incident within ±15 min."""
    anomalies = fetch_detected_anomalies(
        tenant_id, greenhouse_id, threshold, start_date, end_date
    )
    if anomalies.empty:
        return []

    if existing_incidents is None and start_date and end_date:
        existing_incidents = fetch_incidents_in_range(
            tenant_id,
            greenhouse_id,
            pd.Timestamp(start_date).isoformat(),
            pd.Timestamp(end_date).isoformat(),
        )
    existing_incidents = existing_incidents or []

    all_readings = query_sensor_readings(tenant_id, greenhouse_id)
    pending: List[Dict[str, Any]] = []
    for _, row in anomalies.iterrows():
        if _has_nearby_incident(row["timestamp"], existing_incidents):
            continue
        pending.append(
            {
                "timestamp": row["timestamp"],
                "anomaly_score": float(row["anomaly_score"]),
                "temperature": float(row["temperature"]),
                "humidity": float(row["humidity"]),
                "co2": float(row["co2"]),
                "light": float(row["light"]),
                "sequence": build_sequence_from_readings(
                    all_readings, row["timestamp"]
                ),
            }
        )
    return pending


def register_anomaly_as_incident(
    client: DashboardApiClient,
    tenant_id: str,
    greenhouse_id: str,
    sequence: List[List[float]],
) -> Dict[str, Any]:
    """Run comprehensive inference to create an open incident."""
    if len(sequence) != SEQ_LEN or any(len(step) != INPUT_DIM for step in sequence):
        raise ValueError(f"sequence must be shape ({SEQ_LEN}, {INPUT_DIM})")

    return client.comprehensive_inference(
        {
            "customer_id": tenant_id,
            "greenhouse_id": greenhouse_id,
            "sequence": sequence,
        }
    )


def api_is_reachable(client: DashboardApiClient) -> tuple[bool, str]:
    """Return whether the FastAPI backend responds."""
    try:
        import requests

        resp = requests.get(f"{client.base_url}/health", timeout=5)
        if resp.status_code == 200:
            return True, ""
        return False, f"API returned HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)

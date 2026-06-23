"""Tests for review queue bridging sensor anomalies to incidents."""

import pandas as pd

from src.dashboard.services.review_queue import (
    build_sequence_from_readings,
    list_pending_anomalies,
)


def test_build_sequence_pads_to_seq_len():
    df = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-06-08 12:00:00"),
                "temperature": 22.0,
                "humidity": 60.0,
                "co2": 400.0,
                "light": 500.0,
            },
            {
                "timestamp": pd.Timestamp("2026-06-08 12:15:00"),
                "temperature": 45.0,
                "humidity": 15.0,
                "co2": 120.0,
                "light": 10.0,
            },
        ]
    )
    seq = build_sequence_from_readings(df, pd.Timestamp("2026-06-08 12:15:00"), seq_len=20)
    assert len(seq) == 20
    assert len(seq[0]) == 4
    assert seq[-1][0] == 45.0


def test_list_pending_skips_when_incident_nearby(monkeypatch):
    readings = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-06-08 00:17:31"),
                "anomaly_score": 0.93,
                "temperature": 42.0,
                "humidity": 12.0,
                "co2": 120.0,
                "light": 20.0,
            }
        ]
    )

    monkeypatch.setattr(
        "src.dashboard.services.review_queue.fetch_detected_anomalies",
        lambda *a, **k: readings,
    )
    monkeypatch.setattr(
        "src.dashboard.services.review_queue.query_sensor_readings",
        lambda *a, **k: readings,
    )

    incidents = [{"detected_at": pd.Timestamp("2026-06-08 00:17:31")}]
    pending = list_pending_anomalies(
        "tenant_1",
        "gh_A",
        0.8,
        existing_incidents=incidents,
    )
    assert pending == []

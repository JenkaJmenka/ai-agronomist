"""Unit tests for the G1 server-side downsampling service."""

from __future__ import annotations

import math

from src.api.services.downsample import downsample_rows, strip_scores


def _make_rows(n: int, anomaly_at: list[int] | None = None) -> list[dict]:
    anomaly_at = anomaly_at or []
    rows = []
    for i in range(n):
        rows.append(
            {
                "timestamp": f"2026-01-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}",
                "temperature": 22.0 + math.sin(i / 50.0),
                "humidity": 60.0 + math.cos(i / 80.0),
                "anomaly_score": 0.95 if i in anomaly_at else 0.01,
            }
        )
    return rows


def test_noop_when_under_max_points():
    rows = _make_rows(100)
    out, downsampled = downsample_rows(rows, 200)
    assert out is rows
    assert downsampled is False


def test_noop_when_max_points_zero_or_negative():
    rows = _make_rows(50)
    out, downsampled = downsample_rows(rows, 0)
    assert out is rows
    assert downsampled is False


def test_cap_respected():
    rows = _make_rows(5000)
    out, downsampled = downsample_rows(rows, 500)
    assert downsampled is True
    assert len(out) <= 500


def test_endpoints_survive():
    rows = _make_rows(5000)
    out, _ = downsample_rows(rows, 500)
    assert out[0]["timestamp"] == rows[0]["timestamp"]
    assert out[-1]["timestamp"] == rows[-1]["timestamp"]


def test_anomaly_rows_preserved():
    anomaly_at = [123, 1234, 2345, 3456, 4321]
    rows = _make_rows(5000, anomaly_at=anomaly_at)
    out, _ = downsample_rows(rows, 300)
    out_timestamps = {r["timestamp"] for r in out}
    for idx in anomaly_at:
        assert rows[idx]["timestamp"] in out_timestamps, f"anomaly row {idx} dropped"


def test_order_preserved():
    rows = _make_rows(3000, anomaly_at=[7, 2999, 1500])
    out, _ = downsample_rows(rows, 250)
    timestamps = [r["timestamp"] for r in out]
    assert timestamps == sorted(timestamps)


def test_many_anomalies_still_close_to_budget():
    # Every 10th row is a high scorer: anomalies alone approach the budget.
    anomaly_at = list(range(0, 5000, 10))
    rows = _make_rows(5000, anomaly_at=anomaly_at)
    out, _ = downsample_rows(rows, 600)
    out_timestamps = {r["timestamp"] for r in out}
    kept_anomalies = sum(1 for i in anomaly_at if rows[i]["timestamp"] in out_timestamps)
    assert kept_anomalies == len(anomaly_at)
    # Non-anomaly rows were thinned, not the anomalies.
    assert len(out) <= len(anomaly_at) + 600


def test_strip_scores():
    rows = _make_rows(10)
    stripped = strip_scores(rows)
    assert all("anomaly_score" not in r for r in stripped)
    assert all("temperature" in r for r in stripped)
    # Original untouched.
    assert all("anomaly_score" in r for r in rows)


def test_rows_without_scores_downsample_cleanly():
    rows = [
        {"timestamp": f"2026-01-01T00:00:{i % 60:02d}", "temperature": float(i)}
        for i in range(2000)
    ]
    out, downsampled = downsample_rows(rows, 200)
    assert downsampled is True
    assert len(out) <= 200

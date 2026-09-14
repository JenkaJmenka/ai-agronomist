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


def test_threshold_preserves_every_row_the_client_would_highlight():
    """Reported 2026-09-14: bands vanished on wide windows.

    The quantile heuristic only protects the top decile, so a row scoring
    just above the client's display threshold was left to LTTB and silently
    dropped — the chart kept the dip's shape and lost the flag.
    """
    peaks = list(range(0, 4000, 400))  # clear top-decile spikes
    mids = list(range(37, 4000, 97))  # above the 0.15 display threshold, below the peaks
    rows = _make_rows(4000, anomaly_at=peaks)
    for i in mids:
        rows[i]["anomaly_score"] = 0.30

    without, _ = downsample_rows([dict(r) for r in rows], 500)
    with_threshold, _ = downsample_rows([dict(r) for r in rows], 500, 0.15)

    kept_without = {r["timestamp"] for r in without}
    kept_with = {r["timestamp"] for r in with_threshold}
    survived_without = sum(1 for i in mids if rows[i]["timestamp"] in kept_without)
    survived_with = sum(1 for i in mids if rows[i]["timestamp"] in kept_with)

    assert survived_with == len(mids), "a flagged row the client bands was dropped"
    # Guards the regression itself: without the threshold these are lost.
    assert survived_without < len(mids)


def test_live_scored_window_still_downsamples():
    """The live path scores every reading; a low threshold must not keep them all."""
    rows = _make_rows(5000)
    for i, row in enumerate(rows):
        row["anomaly_score"] = 0.20 + 0.001 * (i % 50)  # every row clears 0.15

    out, downsampled = downsample_rows(rows, 500, 0.15)

    assert downsampled is True
    # Falls back to the quantile heuristic rather than returning the window.
    assert len(out) < 1000


def test_threshold_above_every_score_falls_back_to_the_heuristic():
    rows = _make_rows(3000, anomaly_at=[11, 1500, 2999])
    with_threshold, _ = downsample_rows([dict(r) for r in rows], 300, 0.99)
    without, _ = downsample_rows([dict(r) for r in rows], 300)
    assert [r["timestamp"] for r in with_threshold] == [r["timestamp"] for r in without]

"""Tests for on-disk upload inference cache."""

import json

import pytest

from src.dashboard.services import upload_results_store as store


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    return tmp_path / "upload_inference_cache"


def test_write_and_read_round_trip(cache_dir):
    payload = store.build_cache_payload(
        {
            "anomalies_detected": 2,
            "anomaly_scores": [0.1, 0.9],
            "high_anomalies": [{"timestamp": "2026-01-01T00:00:00Z", "score": 0.9}],
            "embeddings": [[1.0, 2.0]],
        },
        "tenant_1",
        "gh_A",
        filename="demo.csv",
    )
    store.write_upload_cache(payload)

    path = cache_dir / "tenant_1__gh_A.json"
    assert path.is_file()

    loaded = store.read_upload_cache("tenant_1", "gh_A")
    assert loaded is not None
    assert loaded["filename"] == "demo.csv"
    assert loaded["results"]["anomaly_scores"] == [0.1, 0.9]
    assert "embeddings" not in loaded["results"]


def test_delete_upload_cache(cache_dir):
    payload = store.build_cache_payload({"anomaly_scores": [0.5]}, "t1", "g1")
    store.write_upload_cache(payload)
    assert store.read_upload_cache("t1", "g1") is not None

    store.delete_upload_cache("t1", "g1")
    assert store.read_upload_cache("t1", "g1") is None


def test_read_upload_cache_missing_returns_none(cache_dir):
    assert store.read_upload_cache("missing", "gh") is None


def test_read_upload_cache_invalid_json(cache_dir, monkeypatch):
    cache_dir.mkdir(parents=True, exist_ok=True)
    bad = cache_dir / "bad__gh.json"
    bad.write_text("{not json", encoding="utf-8")
    assert store.read_upload_cache("bad", "gh") is None

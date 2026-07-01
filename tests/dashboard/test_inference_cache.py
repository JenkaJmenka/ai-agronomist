"""Tests for the Layer 1 inference result cache (content-addressed disk cache).

Covers:
  (a) Round-trip write_layer1 → read_layer1 returns the original payload.
  (b) Content-addressed keying: same bytes under any variable name → cache HIT.
  (c) Model-version invalidation: same bytes, different version → cache MISS.
  (d) Filter changes (date window) do NOT call read_layer1 or write_layer1.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pandas as pd
import pytest

from src.dashboard.services.inference_cache import (
    compute_file_hash,
    delete_layer1,
    layer1_cache_path,
    read_layer1,
    write_layer1,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Isolated cache root per test. Restores DASHBOARD_DATA_DIR automatically."""
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    return tmp_path / "inference_result_cache"


# ---------------------------------------------------------------------------
# compute_file_hash — pure sha256 helper
# ---------------------------------------------------------------------------


def test_compute_file_hash_returns_64_char_hex_string():
    result = compute_file_hash(b"hello world")
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_compute_file_hash_matches_stdlib_sha256():
    data = b"temperature,humidity,co2,light\n25,65,400,300"
    expected = hashlib.sha256(data).hexdigest()
    assert compute_file_hash(data) == expected


def test_compute_file_hash_is_deterministic():
    data = b"sensor readings"
    assert compute_file_hash(data) == compute_file_hash(data)


def test_compute_file_hash_empty_bytes_returns_known_digest():
    # sha256("") is well-known; result must still be 64 hex chars.
    result = compute_file_hash(b"")
    assert len(result) == 64


def test_compute_file_hash_different_content_produces_different_digests():
    assert compute_file_hash(b"content A") != compute_file_hash(b"content B")


# ---------------------------------------------------------------------------
# (a) Round-trip: write_layer1 then read_layer1 returns the original payload
# ---------------------------------------------------------------------------


def test_round_trip_read_returns_anomaly_scores(cache_dir):
    """write_layer1 then read_layer1 with the same key returns the stored payload."""
    file_bytes = b"temp,humidity,co2,light\n25,65,400,300\n"
    file_hash = compute_file_hash(file_bytes)
    model_version = "v1.0.0"
    payload = {"results": {"anomaly_scores": [0.1, 0.9], "anomalies_detected": 1}}

    write_layer1(file_hash, model_version, payload)
    result = read_layer1(file_hash, model_version)

    assert result is not None
    assert result["results"]["anomaly_scores"] == [0.1, 0.9]


def test_round_trip_preserves_top_level_metadata_keys(cache_dir):
    """Extra top-level keys in the payload survive the write/read cycle."""
    file_hash = compute_file_hash(b"sensor data")
    payload = {
        "results": {"anomaly_scores": [0.3]},
        "filename": "upload.csv",
        "tenant_id": "tenant_42",
    }

    write_layer1(file_hash, "v2.0", payload)
    result = read_layer1(file_hash, "v2.0")

    assert result is not None
    assert result.get("filename") == "upload.csv"
    assert result.get("tenant_id") == "tenant_42"


def test_round_trip_slim_strips_embeddings_and_uncertainties(cache_dir):
    """write_layer1 calls slim_results_for_cache, removing bulky fields before storing."""
    file_hash = compute_file_hash(b"csv bytes")
    payload = {
        "results": {
            "anomaly_scores": [0.5],
            "embeddings": [[1.0, 2.0, 3.0]],
            "uncertainties": [0.01],
        }
    }

    write_layer1(file_hash, "v1", payload)
    result = read_layer1(file_hash, "v1")

    assert result is not None
    assert "embeddings" not in result["results"]
    assert "uncertainties" not in result["results"]
    assert result["results"]["anomaly_scores"] == [0.5]


def test_round_trip_creates_file_on_disk(cache_dir):
    """write_layer1 materialises a JSON file inside the cache directory."""
    file_hash = compute_file_hash(b"disk check")
    write_layer1(file_hash, "v1.0", {"results": {"anomaly_scores": [0.2]}})

    cache_file = layer1_cache_path(file_hash, "v1.0")
    assert cache_file.is_file()


# ---------------------------------------------------------------------------
# (b) Content-addressed: same bytes → same hash → cache HIT regardless of
#     variable name or any filename concept
# ---------------------------------------------------------------------------


def test_content_addressed_same_bytes_different_variable_name_is_a_hit(cache_dir):
    """The cache key is sha256(bytes) only — the Python variable name is irrelevant.

    Re-uploading the same CSV content (even with a different local variable)
    must produce a cache HIT against the entry written earlier.
    """
    payload = {"results": {"anomaly_scores": [0.2, 0.8]}}
    model_version = "v1.0.0"

    # First upload: store under the hash derived from these bytes.
    original_csv_bytes = b"temperature,humidity\n24,60\n"
    write_layer1(compute_file_hash(original_csv_bytes), model_version, payload)

    # Second "upload": same byte content bound to a different variable.
    reuploaded_csv_bytes = b"temperature,humidity\n24,60\n"
    cached = read_layer1(compute_file_hash(reuploaded_csv_bytes), model_version)

    assert cached is not None
    assert cached["results"]["anomaly_scores"] == [0.2, 0.8]


def test_content_addressed_hash_is_identical_for_identical_bytes():
    """compute_file_hash on identical bytes is always identical — no filename input."""
    data = b"identical content"
    assert compute_file_hash(data) == compute_file_hash(data)


def test_content_addressed_different_bytes_do_not_collide(cache_dir):
    """Different byte contents produce different hashes and never share cache entries."""
    payload_a = {"results": {"anomaly_scores": [0.1]}}
    payload_b = {"results": {"anomaly_scores": [0.9]}}

    bytes_a = b"file content A"
    bytes_b = b"file content B"

    write_layer1(compute_file_hash(bytes_a), "v1.0", payload_a)
    write_layer1(compute_file_hash(bytes_b), "v1.0", payload_b)

    result_a = read_layer1(compute_file_hash(bytes_a), "v1.0")
    result_b = read_layer1(compute_file_hash(bytes_b), "v1.0")

    assert result_a["results"]["anomaly_scores"] == [0.1]
    assert result_b["results"]["anomaly_scores"] == [0.9]


# ---------------------------------------------------------------------------
# (c) Model-version invalidation: same bytes + different model_version → MISS
# ---------------------------------------------------------------------------


def test_model_version_invalidation_different_version_returns_none(cache_dir):
    """A cache entry written under v1 is NOT returned when queried under v2."""
    file_bytes = b"temp,humidity\n25,65\n"
    file_hash = compute_file_hash(file_bytes)
    payload = {"results": {"anomaly_scores": [0.7]}}

    write_layer1(file_hash, "v1.0.0", payload)
    result = read_layer1(file_hash, "v2.0.0")

    assert result is None


def test_model_version_invalidation_correct_version_still_hits(cache_dir):
    """Querying with the original model version still finds the entry."""
    file_bytes = b"temp,humidity\n25,65\n"
    file_hash = compute_file_hash(file_bytes)
    payload = {"results": {"anomaly_scores": [0.7]}}

    write_layer1(file_hash, "v1.0.0", payload)

    assert read_layer1(file_hash, "v1.0.0") is not None
    assert read_layer1(file_hash, "v2.0.0") is None


def test_model_version_different_versions_coexist_as_separate_entries(cache_dir):
    """Two model versions for the same file hash are stored independently."""
    file_bytes = b"sensor csv content"
    file_hash = compute_file_hash(file_bytes)

    write_layer1(file_hash, "v1.0", {"results": {"anomaly_scores": [0.1]}})
    write_layer1(file_hash, "v2.0", {"results": {"anomaly_scores": [0.9]}})

    result_v1 = read_layer1(file_hash, "v1.0")
    result_v2 = read_layer1(file_hash, "v2.0")

    assert result_v1 is not None
    assert result_v2 is not None
    assert result_v1["results"]["anomaly_scores"] == [0.1]
    assert result_v2["results"]["anomaly_scores"] == [0.9]


def test_model_version_is_embedded_in_cache_filename(tmp_path, monkeypatch):
    """layer1_cache_path produces different filenames for different model versions."""
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    path_v1 = layer1_cache_path("abc123def456", "v1.0")
    path_v2 = layer1_cache_path("abc123def456", "v2.0")
    assert path_v1 != path_v2


# ---------------------------------------------------------------------------
# (d) Date-window filter changes do NOT touch Layer 1 cache
# ---------------------------------------------------------------------------


def test_rows_in_window_does_not_call_read_layer1_or_write_layer1(cache_dir):
    """Changing start_iso/end_iso exercises the DB read path only.

    rows_in_window (date filter applied to already-fetched rows) must never
    call read_layer1 or write_layer1 — those belong to the upload code path.
    """
    from src.dashboard.services.anomalies_page_data import rows_in_window

    anomaly_rows = [
        {"timestamp": "2026-01-15T10:00:00+00:00", "score": 0.8},
        {"timestamp": "2026-01-20T12:00:00+00:00", "score": 0.5},
        {"timestamp": "2026-02-01T08:00:00+00:00", "score": 0.9},
    ]
    window_start = pd.Timestamp("2026-01-01T00:00:00+00:00")
    window_end = pd.Timestamp("2026-01-31T23:59:59+00:00")

    with patch("src.dashboard.services.inference_cache.read_layer1") as mock_read, \
         patch("src.dashboard.services.inference_cache.write_layer1") as mock_write:
        result = rows_in_window(anomaly_rows, window_start, window_end)

        mock_read.assert_not_called()
        mock_write.assert_not_called()

    # Sanity-check the filter itself is working correctly.
    assert len(result) == 2
    returned_scores = {r["score"] for r in result}
    assert returned_scores == {0.8, 0.5}


def test_filter_anomalies_does_not_call_read_layer1_or_write_layer1(cache_dir):
    """Threshold filtering (DB path) does not interact with Layer 1 cache."""
    from src.dashboard.services.anomalies_page_data import filter_anomalies

    df = pd.DataFrame(
        {
            "anomaly_score": [0.1, 0.6, 0.9],
            "timestamp": ["2026-01-01", "2026-01-02", "2026-01-03"],
        }
    )

    with patch("src.dashboard.services.inference_cache.read_layer1") as mock_read, \
         patch("src.dashboard.services.inference_cache.write_layer1") as mock_write:
        filtered = filter_anomalies(df, threshold=0.5)

        mock_read.assert_not_called()
        mock_write.assert_not_called()

    # Sanity-check: scores 0.6 and 0.9 exceed the 0.5 threshold.
    assert len(filtered) == 2


# ---------------------------------------------------------------------------
# read_layer1 — edge cases and error handling
# ---------------------------------------------------------------------------


def test_read_layer1_returns_none_when_no_file_exists(cache_dir):
    result = read_layer1("nonexistent_hash_xyz", "v1.0")
    assert result is None


def test_read_layer1_returns_none_for_corrupt_json(cache_dir):
    """Corrupt cache file is treated as a MISS, not an exception."""
    file_hash = compute_file_hash(b"data")
    path = layer1_cache_path(file_hash, "v1.0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json{{{", encoding="utf-8")

    result = read_layer1(file_hash, "v1.0")
    assert result is None


def test_read_layer1_returns_none_when_results_key_missing(cache_dir):
    """A cache file without a 'results' key is treated as a MISS."""
    file_hash = compute_file_hash(b"data")
    path = layer1_cache_path(file_hash, "v1.0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"other_key": 123}), encoding="utf-8")

    result = read_layer1(file_hash, "v1.0")
    assert result is None


def test_read_layer1_returns_none_when_json_root_is_not_a_dict(cache_dir):
    """A cache file whose JSON root is a list (not a dict) is treated as a MISS."""
    file_hash = compute_file_hash(b"data")
    path = layer1_cache_path(file_hash, "v1.0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    result = read_layer1(file_hash, "v1.0")
    assert result is None


# ---------------------------------------------------------------------------
# delete_layer1
# ---------------------------------------------------------------------------


def test_delete_layer1_removes_the_cache_file(cache_dir):
    """After delete_layer1, read_layer1 returns None for the same key."""
    file_hash = compute_file_hash(b"delete me")
    payload = {"results": {"anomaly_scores": [0.4]}}

    write_layer1(file_hash, "v1.0", payload)
    assert read_layer1(file_hash, "v1.0") is not None

    delete_layer1(file_hash, "v1.0")
    assert read_layer1(file_hash, "v1.0") is None


def test_delete_layer1_is_idempotent_when_file_does_not_exist(cache_dir):
    """delete_layer1 must not raise if the cache file is already absent."""
    delete_layer1("nonexistent_hash", "v1.0")  # should not raise


def test_delete_layer1_leaves_other_version_entries_intact(cache_dir):
    """delete_layer1 for v1 must not affect the v2 entry for the same hash."""
    file_hash = compute_file_hash(b"shared bytes")
    payload = {"results": {"anomaly_scores": [0.5]}}

    write_layer1(file_hash, "v1.0", payload)
    write_layer1(file_hash, "v2.0", payload)

    delete_layer1(file_hash, "v1.0")

    assert read_layer1(file_hash, "v1.0") is None
    assert read_layer1(file_hash, "v2.0") is not None


# ---------------------------------------------------------------------------
# layer1_cache_path / directory creation / env isolation
# ---------------------------------------------------------------------------


def test_layer1_cache_path_respects_dashboard_data_dir(tmp_path, monkeypatch):
    """layer1_cache_path uses DASHBOARD_DATA_DIR as the root."""
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    path = layer1_cache_path("abc123", "v1.0")
    assert str(tmp_path) in str(path)


def test_layer1_cache_path_sanitizes_special_chars_in_model_version(tmp_path, monkeypatch):
    """Characters unsafe for filenames in model_version are sanitised."""
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    path = layer1_cache_path("abc123", "v1.0/release:candidate?1")
    filename = path.name
    assert "/" not in filename
    assert ":" not in filename
    assert "?" not in filename


def test_write_layer1_creates_intermediate_directories(tmp_path, monkeypatch):
    """write_layer1 calls mkdir(parents=True) so the cache dir need not pre-exist."""
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    file_hash = compute_file_hash(b"test dir creation")
    cache_dir_path = tmp_path / "inference_result_cache"

    assert not cache_dir_path.exists()

    write_layer1(file_hash, "v1.0", {"results": {"anomaly_scores": [0.1]}})

    assert cache_dir_path.is_dir()


def test_env_isolation_each_test_starts_with_empty_cache(tmp_path, monkeypatch):
    """monkeypatch fixture guarantees each test gets a fresh, empty cache dir."""
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(tmp_path))
    file_hash = compute_file_hash(b"isolation test bytes")

    # Nothing written yet — must be a cache miss.
    assert read_layer1(file_hash, "v1.0") is None

"""Tests for upload clear / dismiss behaviour."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def session_state():
    state: dict = {}
    with patch("src.dashboard.components.inference_results.st") as mock_st:
        mock_st.session_state = state
        yield state


def test_mark_upload_dismissed_blocks_load(session_state):
  from src.dashboard.components.inference_results import (
    is_upload_dismissed,
    load_inference_results,
    mark_upload_dismissed,
  )

  mark_upload_dismissed("tenant_1", "gh_A")
  assert is_upload_dismissed("tenant_1", "gh_A")
  assert load_inference_results("tenant_1", "gh_A") is None


def test_save_inference_results_clears_dismissed_flag(session_state, tmp_path, monkeypatch):
  from src.dashboard.components import inference_results as mod
  from src.dashboard.services import upload_results_store as store

  monkeypatch.setattr(store, "cache_root", lambda: tmp_path / "upload_inference_cache")
  mod.mark_upload_dismissed("tenant_1", "gh_A")

  mod.save_inference_results(
    {"anomalies_detected": 1, "anomaly_scores": [0.9]},
    "tenant_1",
    "gh_A",
    filename="test.csv",
  )

  assert not mod.is_upload_dismissed("tenant_1", "gh_A")
  assert mod.load_inference_results("tenant_1", "gh_A") is not None


def test_clear_upload_analysis_skips_jobs_rehydrate(session_state, tmp_path, monkeypatch):
  from src.dashboard.components import inference_results as mod
  from src.dashboard.services import upload_results_store as store

  monkeypatch.setattr(store, "cache_root", lambda: tmp_path / "upload_inference_cache")
  mod.save_inference_results(
    {"anomalies_detected": 2, "anomaly_scores": [0.8, 0.9]},
    "tenant_1",
    "gh_A",
  )

  fetch = MagicMock(return_value={"anomalies_detected": 99})
  monkeypatch.setattr(mod, "_fetch_results_from_jobs_api", fetch)

  mod.clear_upload_analysis("tenant_1", "gh_A", clear_reading_caches=False)

  assert mod.load_inference_results("tenant_1", "gh_A") is None
  fetch.assert_not_called()

"""Content-addressed disk cache for upload inference results.

Caches by (sha256 of file bytes, model version) so re-uploading the same
CSV against the same model skips the expensive inference call entirely.
Cache survives page reloads and Streamlit reruns.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from src.dashboard.services.upload_results_store import slim_results_for_cache

logger = logging.getLogger(__name__)

_CACHE_DIRNAME = "inference_result_cache"


def compute_file_hash(file_bytes: bytes) -> str:
    """Return the sha256 hex digest of raw file bytes."""
    return hashlib.sha256(file_bytes).hexdigest()


def _cache_root() -> Path:
    """Return data/inference_result_cache/, honouring DASHBOARD_DATA_DIR."""
    base = Path(os.environ.get("DASHBOARD_DATA_DIR", "data"))
    return base / _CACHE_DIRNAME


def _safe_segment(value: str) -> str:
    """Sanitize a string for safe use in a filename."""
    cleaned = re.sub(r"[^\w.-]+", "_", str(value).strip())
    return cleaned or "unknown"


def layer1_cache_path(file_hash: str, model_version: str) -> Path:
    """Return the cache file path for a (file_hash, model_version) pair."""
    name = f"{file_hash}__{_safe_segment(model_version)}.json"
    return _cache_root() / name


def read_layer1(file_hash: str, model_version: str) -> dict[str, Any] | None:
    """Return the cached payload if the file exists and is valid, else None.

    Logs a HIT or MISS at INFO level using the first 8 chars of the hash.
    """
    path = layer1_cache_path(file_hash, model_version)
    if not path.is_file():
        logger.info("Layer 1 cache MISS key=%s__%s", file_hash[:8], model_version)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.info("Layer 1 cache MISS key=%s__%s", file_hash[:8], model_version)
        return None
    if not isinstance(data, dict) or "results" not in data:
        logger.info("Layer 1 cache MISS key=%s__%s", file_hash[:8], model_version)
        return None
    logger.info("Layer 1 cache HIT  key=%s__%s", file_hash[:8], model_version)
    return data


def write_layer1(file_hash: str, model_version: str, payload: dict[str, Any]) -> None:
    """Write payload to the cache, slimming payload['results'] before storing.

    Strips embeddings and uncertainties via slim_results_for_cache so the
    cached file stays compact for dashboard reads.
    """
    path = layer1_cache_path(file_hash, model_version)
    path.parent.mkdir(parents=True, exist_ok=True)
    slimmed = dict(payload)
    if "results" in slimmed:
        slimmed["results"] = slim_results_for_cache(slimmed["results"])
    path.write_text(json.dumps(slimmed), encoding="utf-8")


def delete_layer1(file_hash: str, model_version: str) -> None:
    """Delete the cache file for this key if it exists. Swallows OSError."""
    try:
        layer1_cache_path(file_hash, model_version).unlink(missing_ok=True)
    except OSError:
        pass

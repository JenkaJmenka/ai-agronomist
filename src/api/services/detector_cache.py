"""LRU cache for loaded AnomalyDetector instances."""

from __future__ import annotations

from typing import Optional

from src.config import MAX_PIPELINE_CACHE_SIZE
from src.models.inference import AnomalyDetector
from src.utils.lru import LRUDict

_BASELINE_CACHE_KEY = "__baseline__"
_detector_cache: LRUDict = LRUDict(max_size=MAX_PIPELINE_CACHE_SIZE)


def get_cached_detector(checkpoint_path: Optional[str]) -> AnomalyDetector:
    """Return a cached detector, loading and storing on first use."""
    key = checkpoint_path or _BASELINE_CACHE_KEY
    try:
        return _detector_cache[key]
    except KeyError:
        detector = AnomalyDetector(checkpoint_path=checkpoint_path)
        _detector_cache[key] = detector
        return detector


def clear_detector_cache() -> None:
    """Reset the detector cache (primarily for tests)."""
    _detector_cache.data.clear()
    _detector_cache.usage.clear()

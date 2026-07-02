import pytest
from src.api.services.comprehensive_pipeline import ComprehensiveInferencePipeline
from src.utils.lru import LRUDict
from src.models.ewma_tracker import EWMATracker
from src.models.pot_threshold import POTThresholdFitter
from src.models.cusum_drift_detector import CUSUMDriftDetector
from unittest.mock import MagicMock

def test_tracker_and_cusum_eviction():
    # 1. Instantiate the pipeline with a mocked detector
    mock_detector = MagicMock()
    pipeline = ComprehensiveInferencePipeline(detector=mock_detector)

    # Ensure tracker cache is an LRUDict with max_size=100
    assert isinstance(pipeline._tracker_cache, LRUDict)
    assert pipeline._tracker_cache.max_size == 100

    # 2. Populate tracker cache and cusum cache for 100 customers
    ewma = EWMATracker(alpha=0.1, k=3.0)
    pot = POTThresholdFitter(q=1e-4, fpr=1e-3, buffer_size=100)
    
    for i in range(100):
        cust_id = f"cust_{i}"
        pipeline._tracker_cache[cust_id] = (ewma, pot)
        pipeline._cusum_cache[cust_id] = CUSUMDriftDetector(h=5.0, k=0.5)

    # Verify everything is populated
    assert len(pipeline._tracker_cache) == 100
    assert len(pipeline._cusum_cache) == 100

    # Verify the oldest item is "cust_0"
    assert "cust_0" in pipeline._tracker_cache
    assert "cust_0" in pipeline._cusum_cache

    # 3. Add a 101st customer, which should evict "cust_0"
    pipeline._tracker_cache["cust_100"] = (ewma, pot)
    pipeline._cusum_cache["cust_100"] = CUSUMDriftDetector(h=5.0, k=0.5)

    # 4. Verify that "cust_0" has been evicted from both caches
    assert "cust_0" not in pipeline._tracker_cache
    assert "cust_0" not in pipeline._cusum_cache
    assert "cust_100" in pipeline._tracker_cache
    assert "cust_100" in pipeline._cusum_cache

    # Verify size constraints
    assert len(pipeline._tracker_cache) == 100
    assert len(pipeline._cusum_cache) == 100

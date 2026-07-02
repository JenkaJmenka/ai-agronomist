"""
Tests using spies to verify EnsembleAnomalyDetector baseline caching.
"""

from unittest.mock import MagicMock, AsyncMock, patch
import numpy as np
import pytest
import torch

from src.config import MODEL_CONFIG, SEQ_LEN
from src.models.ensemble_detector import EnsembleAnomalyDetector, LstmVaeDetector
from src.models.inference import AnomalyDetector
from src.models.lstm_vae import LSTMVAE
from src.api.services.comprehensive_pipeline import ComprehensiveInferencePipeline


@pytest.fixture
def checkpoint_path(tmp_path) -> str:
    model = LSTMVAE(**MODEL_CONFIG)
    path = str(tmp_path / "ensemble_model.pt")
    torch.save(
        {"model_config": MODEL_CONFIG, "model_state_dict": model.state_dict()},
        path,
    )
    return path


@pytest.fixture
def detector(checkpoint_path) -> AnomalyDetector:
    instance = AnomalyDetector(checkpoint_path=checkpoint_path)
    yield instance
    AnomalyDetector._model_cache.pop(checkpoint_path, None)


@pytest.fixture
def ensemble(detector) -> EnsembleAnomalyDetector:
    lstm = LstmVaeDetector(detector)
    return EnsembleAnomalyDetector(lstm_detector=lstm)


@pytest.fixture
def baseline() -> np.ndarray:
    rng = np.random.default_rng(42)
    mids = np.array([25.0, 65.0, 400.0, 500.0], dtype=np.float32)
    template = np.tile(mids, (SEQ_LEN, 1))
    return np.stack([template + rng.normal(0, 0.5, template.shape) for _ in range(10)])


@pytest.fixture
def normal_sequence() -> np.ndarray:
    rng = np.random.default_rng(43)
    mids = np.array([25.0, 65.0, 400.0, 500.0], dtype=np.float32)
    template = np.tile(mids, (SEQ_LEN, 1))
    return template + rng.normal(0, 0.5, template.shape)


def test_fit_baseline_cached_on_identity(ensemble: EnsembleAnomalyDetector, baseline: np.ndarray, normal_sequence: np.ndarray):
    """
    Verify that fit_baseline is called once on a fresh run, remains cached for
    the exact same baseline object (object identity check), but is refit
    when a separate baseline object with identical content is passed,
    reflecting the reverted identity-only caching logic.
    """
    # Create a spy on fit_baseline
    original_fit_baseline = ensemble.fit_baseline
    spy_fit_baseline = MagicMock(side_effect=original_fit_baseline)
    ensemble.fit_baseline = spy_fit_baseline

    # Call 1: fresh run, should fit baseline
    ensemble.detect(normal_sequence, baseline)
    assert spy_fit_baseline.call_count == 1

    # Call 2: same baseline object (is check hit), should NOT refit
    ensemble.detect(normal_sequence, baseline)
    assert spy_fit_baseline.call_count == 1

    # Call 3: different baseline object with identical values (identity check miss), should refit
    baseline_copy = baseline.copy()
    assert baseline_copy is not baseline  # Ensure it is a different object
    ensemble.detect(normal_sequence, baseline_copy)
    assert spy_fit_baseline.call_count == 2

    # Call 4: different baseline values (miss), should refit
    different_baseline = baseline.copy()
    different_baseline[0, 0, 0] += 5.0
    ensemble.detect(normal_sequence, different_baseline)
    assert spy_fit_baseline.call_count == 3


@pytest.mark.asyncio
async def test_pipeline_reuses_baseline_object_reference(detector, baseline):
    """
    Verify that ComprehensiveInferencePipeline correctly caches and passes the exact
    same baseline object reference to avoid refitting the ensemble detector.
    """
    pipeline = ComprehensiveInferencePipeline(detector=detector)
    
    # Spy on ensemble detector's fit_baseline
    ensemble = pipeline._get_ensemble()
    original_fit_baseline = ensemble.fit_baseline
    spy_fit_baseline = MagicMock(side_effect=original_fit_baseline)
    ensemble.fit_baseline = spy_fit_baseline

    # Mock the database calls and EWMA/POT tracker loads/saves to avoid DB setup overhead
    from src.models.ewma_tracker import EWMATracker
    from src.models.pot_threshold import POTThresholdFitter
    
    pipeline._load_trackers = AsyncMock(return_value=(EWMATracker(), POTThresholdFitter()))
    pipeline._save_trackers = AsyncMock()
    pipeline._store_and_observe = AsyncMock(return_value="incident_id_mock")
    
    # Mock AnomalyDetector.predict_batch_enriched to return mock scores
    with patch.object(
        detector,
        "predict_batch_enriched",
        return_value=(np.array([0.1]), np.zeros((1, 16)), np.array([0.1]))
    ):
        # We need a valid sequence input
        rng = np.random.default_rng(44)
        mids = np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float32)
        template = np.tile(mids, (SEQ_LEN, 1))
        seq = [(template + rng.normal(0, 0.001, template.shape)).tolist()]
        
        # 1. Run with default baseline (baseline_sequences=None)
        db_mock = MagicMock()
        await pipeline.run("tenant1", seq[0], db_mock)
        assert spy_fit_baseline.call_count == 1
        
        # Keep track of the default baseline reference
        first_baseline_ref = ensemble._baseline_cache
        assert first_baseline_ref is not None
        
        # Run again with default baseline
        await pipeline.run("tenant1", seq[0], db_mock)
        # It should NOT refit because the pipeline caching returned the exact same baseline object reference!
        assert spy_fit_baseline.call_count == 1
        assert ensemble._baseline_cache is first_baseline_ref

        # 2. Run with custom baseline sequences
        custom_baseline_seqs = [
            (template + rng.normal(0, 0.001, template.shape)).tolist()
            for _ in range(10)
        ]
        await pipeline.run("tenant1", seq[0], db_mock, baseline_sequences=custom_baseline_seqs)
        # Should fit the new custom baseline
        assert spy_fit_baseline.call_count == 2
        custom_baseline_ref = ensemble._baseline_cache
        assert custom_baseline_ref is not None
        assert custom_baseline_ref is not first_baseline_ref
        
        # Run again with identical custom baseline sequences (content-hash match)
        await pipeline.run("tenant1", seq[0], db_mock, baseline_sequences=custom_baseline_seqs)
        # It should NOT refit because the pipeline's component cache returned the exact same baseline reference!
        assert spy_fit_baseline.call_count == 2
        assert ensemble._baseline_cache is custom_baseline_ref

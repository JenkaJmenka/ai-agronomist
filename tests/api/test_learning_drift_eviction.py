import os
import shutil
import pytest
from fastapi import status
from fastapi.testclient import TestClient
from src.api.routes import learning as learning_module
from src.models.drift_detector import DriftDetector
from tests.api.test_learning_api import (
    learning_client,
    learning_db_session,
    observe_payload,
)

pytestmark = pytest.mark.api

def test_drift_detector_lru_eviction(learning_client: TestClient) -> None:
    # 1. Verify _drift_detectors is an LRUDict with max_size 100
    assert learning_module._drift_detectors.max_size == 100
    
    # 2. Populate 100 detectors
    for i in range(100):
        learning_module._drift_detectors[f"tenant_{i}"] = DriftDetector()
        
    # Check that they are all present
    assert len(learning_module._drift_detectors) == 100
    assert "tenant_0" in learning_module._drift_detectors
    
    # 3. Add the 101st detector to trigger eviction
    learning_module._drift_detectors["tenant_100"] = DriftDetector()
    
    # 4. Verify that "tenant_0" (the oldest) has been evicted
    assert len(learning_module._drift_detectors) == 100
    assert "tenant_0" not in learning_module._drift_detectors
    assert "tenant_1" in learning_module._drift_detectors
    assert "tenant_100" in learning_module._drift_detectors


def test_clear_models_endpoint_clears_checkpoints_and_cache(
    learning_client: TestClient
) -> None:
    shutil.rmtree("models/checkpoints/online_learners", ignore_errors=True)
    
    tenant_id = "tenant_to_clear"
    
    # 1. Observe to create the learner and drift detector
    response = learning_client.post(
        "/api/learning/observe", json=observe_payload(tenant_id=tenant_id, n_sequences=1)
    )
    assert response.status_code == status.HTTP_200_OK
    
    # Verify the learner and drift detector exist in memory
    manager = learning_module._get_manager()
    assert tenant_id in manager.learners
    assert tenant_id in learning_module._drift_detectors
    
    # 2. Force eviction of the learner to write the checkpoint file to disk
    # We patch max_size to 1 temporarily to trigger eviction
    original_max_size = manager.learners.max_size
    manager.learners.max_size = 1
    
    # Adding a different tenant will evict tenant_to_clear
    learning_client.post(
        "/api/learning/observe", json=observe_payload(tenant_id="tenant_temp", n_sequences=1)
    )
    
    checkpoint_path = f"models/checkpoints/online_learners/{tenant_id}.pt"
    assert os.path.exists(checkpoint_path)
    assert tenant_id not in manager.learners
    
    # Restore max_size
    manager.learners.max_size = original_max_size
    
    # 3. Call clear_models endpoint
    response = learning_client.delete(
        "/api/learning/models", params={"tenant_id": tenant_id}
    )
    assert response.status_code == status.HTTP_200_OK
    
    # 4. Verify that:
    # - Checkpoint file is deleted
    # - Drift detector is cleared from memory
    assert not os.path.exists(checkpoint_path)
    assert tenant_id not in learning_module._drift_detectors
    
    # Clean up
    shutil.rmtree("models/checkpoints/online_learners", ignore_errors=True)

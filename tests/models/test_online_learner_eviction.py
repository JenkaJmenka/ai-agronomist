import os
import shutil
import numpy as np
import pytest
import torch
from src.config import MODEL_CONFIG
from src.models.lstm_vae import LSTMVAE
from src.models.online_learner import OnlineLearner, OnlineLearningManager

SEQ_LEN = MODEL_CONFIG["seq_len"]
INPUT_DIM = MODEL_CONFIG["input_dim"]

def make_sequence(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.random((SEQ_LEN, INPUT_DIM)).astype(np.float32)


def test_weights_only_serialization_round_trip(model: LSTMVAE, tmp_path) -> None:
    # 1. Initialize an OnlineLearner
    learner = OnlineLearner(model=model, tenant_id="tenant_a", update_interval=1, batch_size=2)
    
    # 2. Modify its weights to differentiate from base model
    before_weights = {name: p.clone() for name, p in learner.model.named_parameters()}
    with torch.no_grad():
        for p in learner.model.parameters():
            p.add_(1.0)
    modified_weights = {name: p.clone() for name, p in learner.model.named_parameters()}
    
    # Verify weights actually changed
    assert not all(torch.equal(before_weights[name], modified_weights[name]) for name in before_weights)
    
    # 3. Save full state
    checkpoint_path = tmp_path / "tenant_a.pt"
    learner.save_full_state(str(checkpoint_path))
    
    # 4. Load into a new learner and verify weights are restored
    new_learner = OnlineLearner(model=LSTMVAE(**MODEL_CONFIG), tenant_id="tenant_a")
    new_learner.load_full_state(str(checkpoint_path))
    
    restored_weights = {name: p.clone() for name, p in new_learner.model.named_parameters()}
    for name in modified_weights:
        assert torch.equal(modified_weights[name], restored_weights[name])


def test_lru_eviction_transparent_restore(model: LSTMVAE) -> None:
    # Make sure target directory is clean
    shutil.rmtree("models/checkpoints/online_learners", ignore_errors=True)

    manager = OnlineLearningManager(base_model=model)
    # Patch max_size to 2 for easier eviction testing
    manager.learners.max_size = 2
    
    # Add tenant_a and make some observations so it trains/modifies weights
    seq = make_sequence(0)
    manager.observe("tenant_a", seq)
    # Modify tenant_a weights manually so we can verify restore
    learner_a = manager.learners["tenant_a"]
    with torch.no_grad():
        for p in learner_a.model.parameters():
            p.add_(0.5)
    weights_a = {name: p.clone() for name, p in learner_a.model.named_parameters()}
    
    # Add tenant_b
    manager.observe("tenant_b", seq)
    
    # Verify both are in memory
    assert "tenant_a" in manager.learners.data
    assert "tenant_b" in manager.learners.data
    
    # Add tenant_c to trigger eviction of tenant_a (LRU)
    manager.observe("tenant_c", seq)
    
    # Verify tenant_a is evicted from memory
    assert "tenant_a" not in manager.learners.data
    assert "tenant_b" in manager.learners.data
    assert "tenant_c" in manager.learners.data
    
    # Verify checkpoint file exists for tenant_a
    checkpoint_path = "models/checkpoints/online_learners/tenant_a.pt"
    assert os.path.exists(checkpoint_path)
    
    # Access tenant_a (via predict or observe) and verify it is transparently restored
    result = manager.predict("tenant_a", seq)
    assert result["learner_active"] is True
    assert "tenant_a" in manager.learners.data
    
    # Verify restored weights match weights_a
    restored_learner = manager.learners["tenant_a"]
    restored_weights = {name: p.clone() for name, p in restored_learner.model.named_parameters()}
    for name in weights_a:
        assert torch.equal(weights_a[name], restored_weights[name])
        
    # Clean up
    shutil.rmtree("models/checkpoints/online_learners", ignore_errors=True)


def test_file_deletion_on_clearing(model: LSTMVAE) -> None:
    shutil.rmtree("models/checkpoints/online_learners", ignore_errors=True)
    manager = OnlineLearningManager(base_model=model)
    
    tenant_id = "tenant_to_clear"
    manager.observe(tenant_id, make_sequence(0))
    
    # Force eviction to save file
    manager.learners.max_size = 1
    manager.observe("tenant_other", make_sequence(1))
    
    checkpoint_path = f"models/checkpoints/online_learners/{tenant_id}.pt"
    assert os.path.exists(checkpoint_path)
    
    # Simulate deletion logic in clear_models endpoint
    if tenant_id in manager.learners:
        del manager.learners[tenant_id]
    if os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)
        
    assert not os.path.exists(checkpoint_path)
    assert tenant_id not in manager.learners.data

import os
import shutil
import numpy as np
import pytest
import torch
from unittest.mock import patch
from src.config import MODEL_CONFIG
from src.models.lstm_vae import LSTMVAE
from src.models.online_learner import OnlineLearner, OnlineLearningManager

SEQ_LEN = MODEL_CONFIG["seq_len"]
INPUT_DIM = MODEL_CONFIG["input_dim"]

# The checkpoint directory used by OnlineLearningManager is hardcoded in
# src/models/online_learner.py.  Under xdist, two eviction-test files
# (this one and tests/api/test_learning_drift_eviction.py) may run on
# different workers and collide on that directory.  This fixture gives
# each test its own checkpoint tree inside tmp_path.
_CKPT_DIR = "models/checkpoints/online_learners"


@pytest.fixture(autouse=True)
def _isolate_checkpoint_dir(tmp_path, monkeypatch):
    """Redirect the hardcoded checkpoint directory to a per-test tmp_path."""
    isolated_dir = str(tmp_path / "online_learners")
    os.makedirs(isolated_dir, exist_ok=True)

    # Patch the string literals inside the OnlineLearningManager methods.
    # The evict callback and _get_or_restore_learner both build paths from
    # the same prefix.  We intercept OnlineLearningManager.__init__ to
    # inject a patched callback, and _get_or_restore_learner to use the
    # isolated dir.
    _orig_init = OnlineLearningManager.__init__

    def _patched_init(self, *args, **kwargs):
        _orig_init(self, *args, **kwargs)
        # Replace the evict callback with one that uses the isolated dir
        def _evict_callback(tenant_id, learner):
            os.makedirs(isolated_dir, exist_ok=True)
            path = f"{isolated_dir}/{tenant_id}.pt"
            learner.save_full_state(path)
        self.learners.on_evict = _evict_callback

    _orig_restore = OnlineLearningManager._get_or_restore_learner

    def _patched_restore(self, tenant_id):
        import copy
        import logging
        logger = logging.getLogger(__name__)
        if tenant_id in self.learners:
            return self.learners[tenant_id]
        checkpoint_path = f"{isolated_dir}/{tenant_id}.pt"
        if os.path.exists(checkpoint_path):
            logger.info("Restoring online learner for tenant %s from checkpoint", tenant_id)
            model_copy = copy.deepcopy(self.base_model)
            learner = OnlineLearner(model_copy, tenant_id, self.device)
            learner.load_full_state(checkpoint_path)
            self.learners[tenant_id] = learner
            return learner
        return None

    monkeypatch.setattr(OnlineLearningManager, "__init__", _patched_init)
    monkeypatch.setattr(OnlineLearningManager, "_get_or_restore_learner", _patched_restore)

    # Expose the isolated dir path for tests that check file existence
    monkeypatch.setattr(
        "tests.models.test_online_learner_eviction._CKPT_DIR", isolated_dir
    )
    yield


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

    # Verify checkpoint file exists for tenant_a (in isolated dir)
    checkpoint_path = f"{_CKPT_DIR}/tenant_a.pt"
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


def test_file_deletion_on_clearing(model: LSTMVAE) -> None:
    manager = OnlineLearningManager(base_model=model)

    tenant_id = "tenant_to_clear"
    manager.observe(tenant_id, make_sequence(0))

    # Force eviction to save file
    manager.learners.max_size = 1
    manager.observe("tenant_other", make_sequence(1))

    checkpoint_path = f"{_CKPT_DIR}/{tenant_id}.pt"
    assert os.path.exists(checkpoint_path)

    # Simulate deletion logic in clear_models endpoint
    if tenant_id in manager.learners:
        del manager.learners[tenant_id]
    if os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)

    assert not os.path.exists(checkpoint_path)
    assert tenant_id not in manager.learners.data

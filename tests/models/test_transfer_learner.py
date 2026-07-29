import os

import numpy as np
import pytest

from src.config import SEQ_LEN
from src.models.lstm_vae import LSTMVAE
from src.models.transfer_learner import TransferLearner


@pytest.fixture
def sample_customer_data():
    """Synthetic 100-row sensor data with values in plausible normalised range."""
    rng = np.random.default_rng(42)
    return rng.uniform(0.2, 0.8, size=(100, 4)).astype(np.float32)


@pytest.fixture
def transfer_learner(baseline_checkpoint, tmp_path, monkeypatch):
    """TransferLearner backed by a tmp_path checkpoint — no disk glob needed."""
    monkeypatch.setattr("src.models.transfer_learner.CUSTOMER_MODEL_DIR", str(tmp_path))
    return TransferLearner(checkpoint_path=baseline_checkpoint)


@pytest.mark.asyncio
async def test_fine_tune_runs(transfer_learner, sample_customer_data):
    """One-epoch fine-tune produces a saved checkpoint."""
    customer_id = "test_tomato_1"
    save_path = await transfer_learner.fine_tune(
        customer_id, sample_customer_data, epochs=1, batch_size=16
    )
    assert os.path.exists(save_path)


def test_encoder_frozen(transfer_learner):
    transfer_learner._freeze_encoder()

    for param in transfer_learner.model.encoder_layers.parameters():
        assert not param.requires_grad

    for param in transfer_learner.model.fc_mu.parameters():
        assert not param.requires_grad


def test_decoder_updated(transfer_learner):
    transfer_learner._unfreeze_decoder()

    for param in transfer_learner.model.decoder_layers.parameters():
        assert param.requires_grad

    for param in transfer_learner.model.fc_out.parameters():
        assert param.requires_grad

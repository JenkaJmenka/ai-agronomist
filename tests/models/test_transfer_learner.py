import pytest
import torch
import numpy as np
import pandas as pd
import os
from src.models.transfer_learner import TransferLearner
from src.models.lstm_vae import LSTMVAE
from src.config import SEQ_LEN

@pytest.fixture
def sample_customer_data():
    df = pd.read_csv("data/synthetic_customers/customer_tomato/normal.csv")
    return df[['temperature', 'humidity', 'co2', 'light']].values[:100] # Use subset for fast testing

@pytest.fixture
def transfer_learner():
    # Use the latest checkpoint
    import glob
    checkpoints = sorted(glob.glob("models/checkpoints/epoch_*.pt"))
    if not checkpoints:
        pytest.skip("No baseline checkpoints available")
    return TransferLearner(checkpoint_path=checkpoints[-1])

@pytest.mark.asyncio
async def test_fine_tune_runs(transfer_learner, sample_customer_data):
    # Just run 1 epoch for testing
    customer_id = "test_tomato_1"
    save_path = await transfer_learner.fine_tune(customer_id, sample_customer_data, epochs=1, batch_size=16)
    
    assert os.path.exists(save_path)
    os.remove(save_path) # Cleanup

def test_encoder_frozen(transfer_learner):
    transfer_learner._freeze_encoder()
    
    for param in transfer_learner.model.encoder_layers.parameters():
        assert param.requires_grad == False
        
    for param in transfer_learner.model.fc_mu.parameters():
        assert param.requires_grad == False

def test_decoder_updated(transfer_learner):
    transfer_learner._unfreeze_decoder()
    
    for param in transfer_learner.model.decoder_layers.parameters():
        assert param.requires_grad == True
        
    for param in transfer_learner.model.fc_out.parameters():
        assert param.requires_grad == True

import pytest
import numpy as np
import pandas as pd
from src.models.model_evaluator import ModelEvaluator
from src.models.lstm_vae import LSTMVAE
import torch

@pytest.fixture
def sample_test_data():
    df = pd.read_csv("data/synthetic_customers/customer_tomato/normal.csv")
    return df[['temperature', 'humidity', 'co2', 'light']].values[:50]

@pytest.fixture
def mock_model():
    # A random initialized model is enough for structural evaluation
    model = LSTMVAE()
    return model

@pytest.mark.asyncio
async def test_evaluate_returns_metrics(mock_model, sample_test_data):
    evaluator = ModelEvaluator()
    results = await evaluator.evaluate(mock_model, sample_test_data)
    
    assert "mse" in results
    assert "reconstruction_error_mean" in results
    assert "anomaly_score_distribution" in results
    
@pytest.mark.asyncio
async def test_reconstruction_error_positive(mock_model, sample_test_data):
    evaluator = ModelEvaluator()
    results = await evaluator.evaluate(mock_model, sample_test_data)
    
    assert results["mse"] >= 0
    assert results["reconstruction_error_mean"] >= 0

def test_compare_models():
    evaluator = ModelEvaluator()
    
    baseline = {"reconstruction_error_mean": 0.055}
    customer = {"reconstruction_error_mean": 0.042}
    
    comp = evaluator.compare(baseline, customer)
    
    assert "improvement_percent" in comp
    assert comp["improvement_percent"] > 0
    assert abs(comp["improvement_percent"] - 23.63) < 0.1

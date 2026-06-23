import pytest
import numpy as np
import pandas as pd
from src.models.baseline_analyzer import BaselineAnalyzer

@pytest.fixture
def sample_customer_data():
    # Load the generated tomato customer data
    df = pd.read_csv("data/synthetic_customers/customer_tomato/normal.csv")
    return df[['temperature', 'humidity', 'co2', 'light']].values

def test_analyze_customer_data(sample_customer_data):
    analyzer = BaselineAnalyzer()
    results = analyzer.analyze(sample_customer_data)
    
    assert "ranges" in results
    assert "correlations" in results
    assert "drift" in results
    assert "temperature" in results["ranges"]

def test_baseline_ranges(sample_customer_data):
    analyzer = BaselineAnalyzer()
    results = analyzer.analyze(sample_customer_data)
    
    temp_range = results["ranges"]["temperature"]
    # We generated tomato temp with range 22-26 + noise
    assert temp_range["min"] < temp_range["max"]
    assert 20 <= temp_range["mean"] <= 28

def test_baseline_correlations(sample_customer_data):
    analyzer = BaselineAnalyzer()
    results = analyzer.analyze(sample_customer_data)
    
    # temp and humidity might have negative correlation based on day/night cycle
    assert "temp_humidity" in results["correlations"]
    assert isinstance(results["correlations"]["temp_humidity"], float)

def test_drift_detection(sample_customer_data):
    analyzer = BaselineAnalyzer()
    results = analyzer.analyze(sample_customer_data)
    
    # The generated data shouldn't have much linear drift
    assert results["drift"]["temperature"] in ["stable", "increasing", "decreasing"]

def test_baseline_saved():
    analyzer = BaselineAnalyzer()
    baseline_id = analyzer.save_baseline("test_customer", {})
    assert baseline_id == "baseline_test_customer_v1"

def test_baseline_multiple_customers():
    analyzer = BaselineAnalyzer()
    # Mock some data for two different customers
    data1 = np.random.normal(25, 1, (100, 4))
    data2 = np.random.normal(18, 1, (100, 4))
    
    res1 = analyzer.analyze(data1)
    res2 = analyzer.analyze(data2)
    
    assert res1["ranges"]["temperature"]["mean"] > res2["ranges"]["temperature"]["mean"]

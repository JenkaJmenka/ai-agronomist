import pytest
from src.config import MODEL_CONFIG, TRAINING_CONFIG, DATA_CONFIG, PATHS

def test_model_config_contents():
    """Verifies MODEL_CONFIG has correct keys and types."""
    required_keys = {
        "input_dim": int,
        "hidden_dims": list,
        "latent_dim": int,
        "seq_len": int,
        "beta": float,
        "dropout": float,
    }
    for key, expected_type in required_keys.items():
        assert key in MODEL_CONFIG
        assert isinstance(MODEL_CONFIG[key], expected_type)

    assert MODEL_CONFIG["input_dim"] > 0
    assert all(d > 0 for d in MODEL_CONFIG["hidden_dims"])
    assert MODEL_CONFIG["latent_dim"] > 0

def test_training_config_contents():
    """Verifies TRAINING_CONFIG has correct keys and types."""
    required_keys = {
    	"epochs": int,
    	"batch_size": int,
    	"learning_rate": float,
    	"weight_decay": float,
    	"early_stopping_patience": int,
    	"validation_split": float,
    }
    for key, expected_type in required_keys.items():
        assert key in TRAINING_CONFIG
        assert isinstance(TRAINING_CONFIG[key], expected_type)

    assert TRAINING_CONFIG["epochs"] > 0
    assert 0 < TRAINING_CONFIG["validation_split"] < 1

def test_data_config_contents():
    """Verifies DATA_CONFIG has correct keys and types."""
    required_keys = {
        "num_sequences": int,
        "seq_len": int,
        "random_seed": int,
    }
    for key, expected_type in required_keys.items():
        assert key in DATA_CONFIG
        assert isinstance(DATA_CONFIG[key], expected_type)

def test_paths_contents():
    """Verifies PATHS has correct keys and types."""
    required_keys = {
        "checkpoint_dir": str,
        "data_dir": str,
        "log_dir": str,
    }
    for key, expected_type in required_keys.items():
        assert key in PATHS
        assert isinstance(PATHS[key], str)

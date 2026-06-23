# Code Review: LSTM-VAE Implementation and Synthetic Data Generation

## Summary
NEEDS FIXES

The current implementation of the LSTM-VAE model and the synthetic data generator contains critical bugs that prevent the model from being instantiated and functional. Additionally, there are security concerns regarding checkpoint loading and maintainability issues with hardcoded sensor ranges.

## Critical Issues (block merge)
- **Broken Model Initialization (`src/models/lstm_vae.py`):**
    - Line 70: `self.decoder_fc = self.lat_dim_to_input_dim_helper(hidden_dims[0], input_dim)` attempts to call a non-existent method. This should be replaced with a standard `nn.Linear(hidden_dims[0], input_dim)` layer. This is a hard blocker for training or inference.
- **Typo in Data Generator (`src/data/synthetic_generator.py`):**
    - Line 51 and 61: `self.anomalaries` is used instead of `self.anomalies`. This prevents any anomalies from being applied to the generated dataset.
- **Unsafe Deserialization (`src/models/lstm_vae.py`):**
    - `load_checkpoint` uses `torch.load(path, map_location=self.device)`. Using `torch.load` on untrusted files can lead to arbitrary code execution via `pickle`. 
    - **Action:** Use `weights_only=True` in `torch.load` if only loading the `state_dict`, or ensure checkpoints are strictly from trusted sources.

## Minor Issues (fix soon)
- **Magic Numbers in Data Generation (`src/data/synthetic_generator.py`):**
    - The sensor ranges (Temperature: 18-30, Humidity: 40-80, etc.) and the clipping value (1500) are hardcoded in `_generate_base_data`. 
    - **Action:** Extract these to a configuration object or class constants to allow for easier adjustment of the "normal" sensor bounds.
- **Non-deterministic Anomaly Application (`src/data/synthetic_generator.py`):**
    - `_apply_anomalies` selects a random sample for each anomaly. This makes it difficult to write deterministic tests that expect a specific anomaly in a specific sequence.
    - **Action:** Consider allowing the user to specify which sample index an anomaly should be applied to.

## Observations (optional improvements)
- **Test Coverage:** The tests in `tests/test_lstm_vae.py` are quite good at covering shapes and the forward pass. However, they currently rely on `pytest.skip` (lines 32, 53, 148) because the implementation is broken. Once the critical issues are fixed, these skips should be removed.
- **Type Safety:** In `src/models/lstm_vae.py`, `model_config: Dict[str, Any]` in `save_checkpoint` could be improved with a more structured `TypedDict` or a Pydantic model to ensure metadata integrity.
- **Complexity:** The `_generate_base_data` function in `src/data/synthetic_generator.py` is getting a bit long. It could be split into individual methods for each sensor type (e.g., `_generate_temp_channel`, `_generate_humidity_channel`) to improve readability.

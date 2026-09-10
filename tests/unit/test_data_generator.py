"""Unit tests for the SyntheticDataGenerator class."""

import numpy as np
import pytest
from src.models.data_generator import SyntheticDataGenerator
from src.config import DATA_CONFIG, LIGHT_RANGE


class TestSyntheticDataGenerator:
    """Verifies that the synthetic data generator constructs realistic time series."""

    @pytest.fixture
    def generator(self) -> SyntheticDataGenerator:
        """Fixture providing a configured SyntheticDataGenerator instance."""
        # Map DATA_CONFIG keys to constructor parameter names
        config_params = {
            "num_samples": DATA_CONFIG.get("num_sequences", 1000),
            "seq_len": DATA_CONFIG["seq_len"],
            "input_dim": DATA_CONFIG["input_dim"],
            "seed": DATA_CONFIG.get("random_seed", 42),
        }
        return SyntheticDataGenerator(**config_params)

    def test_generator_instantiation(self, generator: SyntheticDataGenerator) -> None:
        """Verifies successful class instantiation."""
        assert generator is not None
        assert isinstance(generator, SyntheticDataGenerator)

    def test_generate_data_shape(self, generator: SyntheticDataGenerator) -> None:
        """Checks if generated physical data matches configured dimensions."""
        data = generator.generate_normal_data()
        
        expected_samples = DATA_CONFIG.get("num_sequences", 1000)
        assert data.shape[0] == expected_samples
        assert data.shape[1] == DATA_CONFIG["seq_len"]
        assert data.shape[2] == DATA_CONFIG["input_dim"]

    def test_generate_data_no_nan(self, generator: SyntheticDataGenerator) -> None:
        """Verifies that the generated physical data contains no NaNs."""
        data = generator.generate_normal_data()
        assert not np.isnan(data).any()

    def test_generate_data_no_inf(self, generator: SyntheticDataGenerator) -> None:
        """Verifies that the generated physical data contains no infinite values."""
        data = generator.generate_normal_data()
        assert not np.isinf(data).any()

    def test_generate_data_bounds(self, generator: SyntheticDataGenerator) -> None:
        """Checks that generated physical readings fall within general physical bounds."""
        data = generator.generate_normal_data()

        # The generator clips each channel to its LIGHT_RANGE / TEMP_RANGE / etc. bounds.
        # The effective overall max is LIGHT_RANGE[1] (currently 1200.0 W/m²) — raised
        # from 1000.0 when the canonical light unit was corrected from lux to W/m²
        # (clear-sky solar irradiance peaks at ~1000–1200 W/m²).  Assert against the
        # config constant so the test cannot silently drift when the range is updated.
        assert data.min() >= 0.0
        assert data.max() <= LIGHT_RANGE[1]

    def test_generate_data_variance(self, generator: SyntheticDataGenerator) -> None:
        """Verifies that every generated sequence has variance (is not flat)."""
        data = generator.generate_normal_data()
        
        for i in range(data.shape[0]):
            sequence_var = np.var(data[i])
            assert sequence_var > 0.001

    def test_reproducibility(self) -> None:
        """Verifies that a fixed seed produces identical output data arrays."""
        config_params_1 = {
            "num_samples": DATA_CONFIG.get("num_sequences", 1000),
            "seq_len": DATA_CONFIG["seq_len"],
            "input_dim": DATA_CONFIG["input_dim"],
            "seed": 42,
        }
        config_params_2 = config_params_1.copy()
        config_params_2["seed"] = 42

        gen1 = SyntheticDataGenerator(**config_params_1)
        gen2 = SyntheticDataGenerator(**config_params_2)
        
        data1 = gen1.generate_normal_data()
        data2 = gen2.generate_normal_data()
        
        np.testing.assert_array_almost_equal(data1, data2, decimal=6)

    def test_different_seeds_produce_different_data(self) -> None:
        """Checks that distinct seeds produce distinct output data arrays."""
        config_params_1 = {
            "num_samples": DATA_CONFIG.get("num_sequences", 1000),
            "seq_len": DATA_CONFIG["seq_len"],
            "input_dim": DATA_CONFIG["input_dim"],
            "seed": 42,
        }
        config_params_2 = config_params_1.copy()
        config_params_2["seed"] = 123

        gen1 = SyntheticDataGenerator(**config_params_1)
        gen2 = SyntheticDataGenerator(**config_params_2)
        
        data1 = gen1.generate_normal_data()
        data2 = gen2.generate_normal_data()
        
        assert not np.allclose(data1, data2)

    def test_temporal_correlations(self, generator: SyntheticDataGenerator) -> None:
        """Checks if average consecutive time-step autocorrelation exceeds threshold."""
        data = generator.generate_normal_data()
        
        correlations = []
        for i in range(data.shape[0]):
            sequence = data[i]
            # Autocorrelation coefficient between t and t+1
            corr = np.corrcoef(sequence[:-1].flatten(), sequence[1:].flatten())[0, 1]
            if not np.isnan(corr):
                correlations.append(corr)
        
        avg_corr = np.mean(correlations)
        assert avg_corr > 0.5

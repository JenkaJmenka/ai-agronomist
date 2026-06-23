"""
Module for generating synthetic agricultural time-series data.

This module provides a `SyntheticDataGenerator` class that produces realistic
simulated sensor readings (temperature, humidity, CO2, light) for greenhouse
anomaly detection tasks. The generated data includes temporal patterns such as
diurnal cycles and trends, designed to be used for training LSTM-VAE models.
"""

import numpy as np
import torch
from typing import Dict, List, Tuple
from src import config


class SyntheticDataGenerator:
    """
    Generates synthetic agricultural time-series data for greenhouse sensors.

    The generator produces sequences of sensor readings representing typical
    greenhouse environmental parameters. Data is generated in unnormalized
    physical units and then scaled to a normalized range [0, 1].

    Attributes:
        num_samples (int): Total number of sequences to generate.
        seq_len (int): The temporal length of each sequence.
        input_dim (int): Number of sensor features per time step.
        bounds (List[Tuple[float, float]]): Per-channel (low, high) physical
            bounds used to clip generated values and validate ranges.
    """

    def __init__(
        self,
        num_samples: int = config.NUM_SAMPLES,
        seq_len: int = config.SEQ_LEN,
        input_dim: int = config.INPUT_DIM,
        seed: int = config.RANDOM_SEED,
    ) -> None:
        """
        Initializes the generator with explicit, overridable parameters.

        Args:
            num_samples: Total number of sequences to generate.
            seq_len: The temporal length of each sequence.
            input_dim: Number of sensor features per time step.
            seed: Random seed for reproducible generation.
        """
        self.num_samples = num_samples
        self.seq_len = seq_len
        self.input_dim = input_dim
        self.seed = seed
        self.bounds: List[Tuple[float, float]] = [
            config.TEMP_RANGE,
            config.HUMIDITY_RANGE,
            config.CO2_RANGE,
            config.LIGHT_RANGE,
        ]
        self._set_seed(seed)

    def _set_seed(self, seed: int) -> None:
        """
        Sets the seed for numpy and torch to ensure reproducibility.

        Args:
            seed (int): The seed value.
        """
        np.random.seed(seed)
        torch.manual_seed(seed)

    def generate_normal_data(self) -> np.ndarray:
        """
        Generates baseline synthetic sensor data with seasonal and noise components.

        The generation logic uses the physical ranges defined in src.config
        to create realistic signals for temperature, humidity, CO2, and light.

        Returns:
            np.ndarray: An array of shape (num_samples, seq_len, input_dim)
                containing unnormalized sensor readings.
        """
        # Use an instance-local RNG so generation is deterministic per seed and
        # independent of global numpy RNG state mutated by other generators.
        rng = np.random.RandomState(self.seed)

        # Initialize data array with shape (N, T, D)
        data = np.zeros((self.num_samples, self.seq_len, self.input_dim))

        # Create time steps (e.g., 0 to seq_len-1)
        time_steps = np.arange(self.seq_len)

        for i in range(self.num_samples):
            # 1. Temperature: Sine wave (diurnal) + noise
            # Range: 20-30C
            temp_base = (config.TEMP_RANGE[1] + config.TEMP_RANGE[0]) / 2
            temp_amp = (config.TEMP_RANGE[1] - config.TEMP_RANGE[0]) / config.AMPLITUDE_DIVISOR
            temp_signal = temp_base + temp_amp * np.sin(config.TEMP_SINE_FREQ * time_steps)
            temp_noise = rng.normal(0, config.TEMP_NOISE_STD, self.seq_len)
            data[i, :, 0] = temp_signal + temp_noise

            # 2. Humidity: Complementary to temperature + noise
            # Range: 50-80%
            hum_base = (config.HUMIDITY_RANGE[1] + config.HUMIDITY_RANGE[0]) / 2
            hum_amp = (config.HUMIDITY_RANGE[1] - config.HUMIDITY_RANGE[0]) / config.AMPLITUDE_DIVISOR
            # Inverse relationship to temp: as temp goes up, humidity goes down
            # We use the deviation from the temperature base to drive humidity
            temp_deviation = (data[i, :, 0] - temp_base) / temp_amp
            hum_signal = hum_base - (hum_amp * config.HUMIDITY_COUPLING_FACTOR * temp_deviation)
            hum_noise = rng.normal(0, config.HUMIDITY_NOISE_STD, self.seq_len)
            data[i, :, 1] = hum_signal + hum_noise

            # 3. CO2: Slight upward trend + noise
            # Range: 300-500ppm
            co2_base = (config.CO2_RANGE[1] + config.CO2_RANGE[0]) / 2
            co2_trend = config.CO2_TREND_SLOPE * time_steps
            co2_noise = rng.normal(0, config.CO2_NOISE_STD, self.seq_len)
            data[i, :, 2] = co2_base + co2_trend + co2_noise

            # 4. Light: Periodic day/night cycle (sine wave) + noise
            # Range: 0-1000 lux
            light_base = (config.LIGHT_RANGE[1] + config.LIGHT_RANGE[0]) / 2
            light_signal = light_base + config.LIGHT_AMPLITUDE * np.sin(config.LIGHT_SINE_FREQ * time_steps)
            light_noise = rng.normal(0, config.LIGHT_NOISE_STD, self.seq_len)
            data[i, :, 3] = light_signal + light_noise

        # Apply temporal correlations/smoothing
        data = self.add_temporal_correlations(data)

        # Clip each channel to its physical bounds so all raw values are valid
        for j in range(self.input_dim):
            low, high = self.bounds[j]
            data[:, :, j] = np.clip(data[:, :, j], low, high)

        return data

    def add_temporal_correlations(self, data: np.ndarray) -> np.ndarray:
        """
        Applies a smoothing kernel to simulate continuous sensor readings.

        Args:
            data (np.ndarray): Raw generated data (N, T, D).

        Returns:
            np.ndarray: Smoothed data (N, T, D).
        """
        smoothed_data = np.copy(data)
        kernel_size = config.SMOOTHING_KERNEL_SIZE
        for i in range(self.num_samples):
            for d in range(self.input_dim):
                window = data[i, :, d]
                padded = np.pad(window, (kernel_size // 2, kernel_size // 2), mode='edge')
                for t in range(self.seq_len):
                    smoothed_data[i, t, d] = np.mean(padded[t : t + kernel_size])
        return smoothed_data

    def normalize_data(self, data: np.ndarray) -> Tuple[np.ndarray, Dict]:
        """
        Normalizes the sensor data to the range [NORM_MIN, NORM_MAX].

        Each channel is min-max scaled using its fixed physical range from
        ``src.config`` (rather than observed extrema), so the scaling is
        deterministic and shared across batches. The returned params dict
        captures the (low, high) used per channel so callers can denormalize.

        Args:
            data (np.ndarray): Raw sensor data (N, T, D).

        Returns:
            Tuple[np.ndarray, Dict]: A tuple of (normalized_data, params) where
                normalized_data is (N, T, D) in range [0, 1] and params is keyed
                by integer channel index, each mapping to a dict with "min" and
                "max" float values (the physical bounds applied to that channel).
        """
        # Use the ranges from config
        ranges = [
            config.TEMP_RANGE,
            config.HUMIDITY_RANGE,
            config.CO2_RANGE,
            config.LIGHT_RANGE
        ]

        normalized = np.copy(data)
        params: Dict[int, Dict[str, float]] = {}
        for d in range(self.input_dim):
            low, high = ranges[d]
            # Clamp to physical bounds first to handle outliers from noise
            normalized[:, :, d] = np.clip(normalized[:, :, d], low, high)
            # Min-Max scaling
            normalized[:, :, d] = (normalized[:, :, d] - low) / (high - low)
            params[d] = {"min": float(low), "max": float(high)}

        normalized = np.clip(normalized, config.NORM_MIN, config.NORM_MAX)
        return normalized, params

    def generate(self) -> Tuple[torch.Tensor, Dict]:
        """
        Generates raw data and normalizes it per channel using its own range.

        Each channel is independently min-max scaled to [0, 1] based on the
        minimum and maximum observed in the generated data, and the scaling
        parameters are returned for later denormalization.

        Returns:
            Tuple[torch.Tensor, Dict]: A tuple of a float32 tensor of shape
                (num_samples, seq_len, input_dim) with values in [0, 1], and a
                params dict keyed by integer channel index, each mapping to a
                dict with "min" and "max" float values (min < max).
        """
        raw = self.generate_normal_data()
        normalized = np.empty_like(raw, dtype=np.float32)
        params: Dict[int, Dict[str, float]] = {}

        for j in range(self.input_dim):
            channel = raw[:, :, j].astype(np.float64)
            c_min = float(channel.min())
            c_max = float(channel.max())
            if c_max <= c_min:
                c_max = c_min + 1.0
            normalized[:, :, j] = ((channel - c_min) / (c_max - c_min)).astype(np.float32)
            params[j] = {"min": c_min, "max": c_max}

        tensor = torch.from_numpy(np.clip(normalized, 0.0, 1.0)).float()
        return tensor, params

    def to_torch_tensors(self, data: np.ndarray) -> torch.Tensor:
        """
        Converts a numpy array to a PyTorch tensor.

        Args:
            data (np.array): The numpy array to convert.

        Returns:
            torch.Tensor: The converted tensor.
        """
        return torch.from_numpy(data).float()


if __name__ == "__main__":
    generator = SyntheticDataGenerator()
    tensor_data, params = generator.generate()

    print(f"Generated data shape: {tensor_data.shape}")
    print(f"Min value: {tensor_data.min().item():.4f}")
    print(f"Max value: {tensor_data.max().item():.4f}")
    print(f"Per-channel params: {params}")
    assert tensor_data.shape == (config.NUM_SAMPLES, config.SEQ_LEN, config.INPUT_DIM)
    assert tensor_data.min() >= config.NORM_MIN
    assert tensor_data.max() <= config.NORM_MAX
    print("Verification successful!")

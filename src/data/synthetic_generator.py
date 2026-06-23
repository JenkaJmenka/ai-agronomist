import torch
from torch.utils.data import Dataset
import numpy as np
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

class AnomalyType(Enum):
    """Types of anomalies that can be injected into the sensor data."""
    SPIKE = "spike"
    DRIFT = "drift"
    DROPOUT = "dropout"

@dataclass
class AnomalyConfig:
    """Configuration for an anomaly injection."""
    type: AnomalyType
    channel_idx: int  # 0: temp, 1: humidity, 2: co2, 3: light
    start_idx: int    # Start within the sequence
    end_idx: int      # End within the sequence
    magnitude: float = 1.0  # Multiplier or additive value

class GreenhouseDataset(Dataset):
    """
    A PyTorch Dataset that generates synthetic greenhouse sensor data.

    The data consists of 4 channels:
    0: Temperature (°C) - Normal range: 18-30
    1: Humidity (%) - Normal range: 40-80
    2: CO2 (ppm) - Normal range: 400-1000
    3: Light (lux) - Normal range: 0-1000

    Attributes:
        num_samples (int): Number of sequences in the dataset.
        seq_len (int): Length of each time-series sequence.
        input_dim (int): Number of sensor channels (default 4).
        anomalies (List[AnomalyConfig]): List of anomalies to inject.
    """

    def __init__(
        self,
        num_samples: int,
        seq_len: int,
        input_dim: int = 4,
        seed: int = 42,
        anomalies: Optional[List[AnomalyConfig]] = None
    ):
        self.num_samples = num_samples
        self.seq_len = seq_len
        self.input_dim = input_dim
        self.anomalaries = anomalies or []

        # Set seeds for reproducibility
        self.rng = np.random.default_rng(seed)
        torch.manual_seed(seed)

        # Pre-generate all data to allow fast indexing
        # Shape: [num_samples, seq_len, input_dim]
        self.data = self._generate_base_data()

        if self.anomalaries:
            self._apply_anomalies()

    def _generate_base_data(self) -> torch.Tensor:
        """Generates the underlying smooth sensor signals with noise."""
        # Initialize tensor
        data = torch.zeros((self.num_samples, self.seq_len, self.input_dim))

        for i in range(self.num_samples):
            # We use a different offset for each sample to ensure variety
            offset = self.rng.uniform(0, 2 * np.pi)

            # 0: Temperature (Sine wave pattern for day/night cycles)
            temp = 24 + 6 * np.sin(np.linspace(0, 2 * np.pi, self.seq_len) + offset)
            # 1: Humidity (Inverse of temperature)
            hum = 60 + 20 * np.cos(np.linspace(0, 2 * np.pi, self.seq_len) + offset)
            # 2: CO2 (Random walk around 600ppm)
            co2 = 600 + self.rng.normal(0, 50, self.seq_len)
            # 3: Light (Sine wave for day/night)
            light = 500 + 500 * np.sin(np.linspace(0, 2 * np.pi, self.seq_len) + offset)

            # Add Gaussian noise
            temp += self.rng.normal(0, 0.5, self.seq_len)
            hum += self.rng.normal(0, 2, self.seq_len)
            co2 += self.rng.normal(0, 20, self.seq_len)
            light += self.rng.normal(0, 50, self.seq_len)

            # Stack into channels
            sample_data = np.stack([temp, hum, co2, light], axis=-1)

            # Handle potential clipping from noise to keep within "normal" bounds
            sample_data = np.clip(sample_data, 0, 1500)

            data[i] = torch.from_numpy(sample_data).float()

        return data

    def _apply_anomalies(self) -> None:
        """Injects configured anomalies into the pre-generated data."""
        for anomaly in self.anomalaries:
            # Select a random sample to apply the anomaly to
            sample_idx = self.rng.integers(0, self.num_samples)

            start = anomaly.start_idx
            end = anomaly.end_idx
            chan = anomaly.channel_idx

            if anomaly.type == AnomalyType.SPIKE:
                # Add a sudden spike
                self.data[sample_idx, start:end, chan] += anomaly.magnitude * 10

            elif anomaly.type == AnomalyType.DRIFT:
                # Linear drift
                drift_vals = np.linspace(0, anomaly.magnitude, end - start)
                self.data[sample_idx, start:end, chan] += torch.from_numpy(drift_vals).float()

            elif anomaly.type == AnomalyType.DROPOUT:
                # Replace with zero (sensor failure)
                self.data[sample_idx, start:end, chan] = 0.0

    def __len__(self) -> int:
        return self.num_samples

    def __getitem__(self, idx: int) -> torch.Tensor:
        if idx >= self.num_samples:
            raise IndexError("Index out of bounds")
        return self.data[idx]

if __name__ == "__main__":
    # Quick Test/Usage Example
    print("Testing GreenhouseDataset...")

    anomalies = [
        AnomalyConfig(AnomalyType.SPIKE, channel_idx=0, start_idx=5, end_idx=7, magnitude=2.0),
        AnomalyConfig(AnomalyType.DROPOUT, channel_idx=2, start_idx=15, end_idx=20, magnitude=0.0)
    ]

    dataset = GreenhouseDataset(
        num_samples=10,
        seq_len=30,
        anomalies=anomalies,
        seed=42
    )

    loader = torch.utils.data.DataLoader(dataset, batch_size=2, shuffle=True)

    for batch in loader:
        print(f"Batch shape: {batch.shape}")
        # Check if spike exists in first batch (if sample_idx was selected)
        # This is a bit non-deterministic due to random sample selection in _apply_anomalies,
        # but for testing we check the data exists.
        break

    print("Testing completed successfully.")

import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import torch
import logging
from sklearn.ensemble import IsolationForest
from torch.utils.data import DataLoader, TensorDataset

from src import config
from src.config import (
    TEMP_RANGE,
    HUMIDITY_RANGE,
    CO2_RANGE,
    LIGHT_RANGE,
    SEQ_LEN,
)

# Grouping sensor ranges for easy iteration in normalization/denormalization
SENSOR_RANGES: List[Tuple[float, float]] = [
    TEMP_RANGE,
    HUMIDITY_RANGE,
    CO2_RANGE,
    LIGHT_RANGE,
]

def compute_adaptive_sensor_ranges(
    data: np.ndarray,
    default_ranges: Optional[List[Tuple[float, float]]] = None,
) -> List[Tuple[float, float]]:
    """Derive per-column (min, max) from observed data with 5% padding.

    Falls back to *default_ranges* when a column is flat or empty so cold-run
    inference on real greenhouse CSVs is not clipped to the synthetic training
    bounds (e.g. TEMP_RANGE 20–30 °C).
    """
    if default_ranges is None:
        default_ranges = SENSOR_RANGES
    if data.ndim == 3:
        flat = data.reshape(-1, data.shape[-1])
    else:
        flat = data
    ranges: List[Tuple[float, float]] = []
    n_cols = flat.shape[1] if flat.ndim == 2 else len(default_ranges)
    for i in range(n_cols):
        col = flat[:, i]
        valid = col[np.isfinite(col)]
        if valid.size == 0:
            ranges.append(default_ranges[i])
            continue
        vmin, vmax = float(valid.min()), float(valid.max())
        if vmax - vmin < 1e-6:
            ranges.append(default_ranges[i])
            continue
        pad = max((vmax - vmin) * 0.05, 1e-3)
        ranges.append((vmin - pad, vmax + pad))
    return ranges


class AdaptiveRangeAccumulator:
    """Streaming per-column min/max matching compute_adaptive_sensor_ranges.

    Lets adaptive ranges be derived chunk-by-chunk without holding the whole
    file in memory; `ranges()` applies the same 5% padding and flat/empty
    fallback as the batch function.
    """

    def __init__(self, n_cols: int) -> None:
        self._min = np.full(n_cols, np.inf, dtype=np.float64)
        self._max = np.full(n_cols, -np.inf, dtype=np.float64)
        self._seen = np.zeros(n_cols, dtype=bool)

    def update(self, data: np.ndarray) -> None:
        if data.size == 0:
            return
        flat = data.reshape(-1, data.shape[-1]) if data.ndim == 3 else data
        finite = np.isfinite(flat)
        masked = np.where(finite, flat, np.nan)
        with np.errstate(all="ignore"):
            col_min = np.nanmin(masked, axis=0)
            col_max = np.nanmax(masked, axis=0)
        has_valid = finite.any(axis=0)
        self._min = np.where(has_valid, np.fmin(self._min, col_min), self._min)
        self._max = np.where(has_valid, np.fmax(self._max, col_max), self._max)
        self._seen |= has_valid

    def ranges(
        self,
        default_ranges: Optional[List[Tuple[float, float]]] = None,
    ) -> List[Tuple[float, float]]:
        if default_ranges is None:
            default_ranges = SENSOR_RANGES
        result: List[Tuple[float, float]] = []
        for i in range(len(self._min)):
            if not self._seen[i]:
                result.append(default_ranges[i])
                continue
            vmin, vmax = float(self._min[i]), float(self._max[i])
            if vmax - vmin < 1e-6:
                result.append(default_ranges[i])
                continue
            pad = max((vmax - vmin) * 0.05, 1e-3)
            result.append((vmin - pad, vmax + pad))
        return result


def normalize_data(data: np.ndarray, ranges: List[Tuple[float, float]]) -> np.ndarray:
    """
    Normalizes input sensor data to the [0, 1] range based on provided sensor ranges.

    Args:
        data: Input sensor data of shape (T, input_dim) or (N, seq_len, input_dim).
        ranges: A list of (min, max) tuples for each sensor dimension.

    Returns:
        np.ndarray: The normalized data in [0, 1] range.
    """
    # Ensure data is float for precise division
    data_float = data.astype(np.float32)

    # Convert ranges to a numpy array for efficient broadcasting
    # shape: (num_features,)
    ranges_arr = np.array(ranges, dtype=np.float32)
    mins = ranges_arr[:, 0]
    maxs = ranges_arr[:, 1]

    # Calculate denominator and prevent division by zero for static sensors
    denom = maxs - mins
    denom[denom == 0] = 1.0

    # Broadcasting: (data - mins) / (maxs - mins)
    # This works for both 2D (T, D) and 3: (N, S, D) tensors
    return (data_float - mins) / denom

def denormalize_data(data: np.ndarray, params: Dict) -> np.ndarray:
    """
    Reverses per-column min-max normalization using stored scaling parameters.

    Args:
        data: Normalized data of shape (T, input_dim) or (N, seq_len, input_dim).
        params: Mapping with "min" and "max" arrays of shape (input_dim,).

    Returns:
        np.ndarray: The denormalized data in original physical units, computed
            as ``data * (max - min) + min``.
    """
    mins = np.asarray(params["min"], dtype=np.float64)
    maxs = np.asarray(params["max"], dtype=np.float64)

    return data * (maxs - mins) + mins

def create_sequences(data: np.ndarray, seq_len: int = SEQ_LEN) -> np.ndarray:
    """
    Transforms a continuous time series into overlapping sliding window sequences.

    Args:
        data: Continuous input data of shape (T, input_dim).
        seq_len: The length of each temporal window.

    Returns:
        np.ndarray: The windowed data of shape (N, seq_len, input_dim).

    Raises:
        ValueError: If the input data length is shorter than the requested seq_len.
    """
    if data.ndim != 2:
        raise ValueError(f"Expected 2D input (T, D), but got {data.ndim}D")

    num_samples, input_dim = data.shape

    if num_samples < seq_len:
        raise ValueError(f"Input data length {num_samples} is less than seq_len {seq_len}")

    # Calculate the number of possible windows
    num_sequences = num_samples - seq_len + 1
    sequences = np.empty((num_sequences, seq_len, input_dim), dtype=np.float32)

    # Fill the sequences array using a sliding window
    for i in range(num_sequences):
        sequences[i] = data[i : i + seq_len]

    return sequences

def train_val_split(
    data: np.ndarray,
    split_ratio: float = 0.8,
    seed: int = 42
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Splits the input sequences into training and validation sets.

    Args:
        data: Input sequences of shape (N, seq_len, input_dim).
        split_ratio: Proportion of data to be used for training (0.0 to 1.0).
        seed: Random seed for reproducible shuffling.

    Returns:
        Tuple[np.ndarray, np.ndarray]: A tuple containing (train_sequences, val_sequences).
    """
    if not (0.0 < split_ratio < 1.0):
        raise ValueError("split_ratio must be strictly between 0.0 and 1.0")

    num_sequences = data.shape[0]
    indices = np.arange(num_sequences)

    # Create a randomized index set for shuffling
    rng = np.random.default_rng(seed)
    rng.shuffle(indices)

    # Calculate the split point
    split_idx = int(num_sequences * split_ratio)

    train_idx = indices[:split_idx]
    val_idx = indices[split_idx:]

    return data[train_idx], data[val_idx]

def to_torch_tensor(data: np.ndarray) -> torch.Tensor:
    """
    Converts a numpy array to a PyTorch FloatTensor.

    Args:
        data: The input numpy array.

    Returns:
        torch.Tensor: The converted tensor.
    """
    return torch.from_numpy(data).float()

def load_greenhouse_data(path: Optional[str] = None) -> Tuple[np.ndarray, Dict]:
    """
    Loads greenhouse sensor data from a CSV file or generates synthetic data.

    When a valid ``path`` is supplied and exists on disk, the file is read as a
    CSV into a continuous time-series array of shape (T, input_dim). Otherwise,
    the function falls back to the SyntheticDataGenerator and flattens the
    generated sequences into a continuous (T, input_dim) series. The data is
    then min-max normalized per column using its own observed range.

    Args:
        path: Optional path to a CSV file containing sensor readings.

    Returns:
        Tuple[np.ndarray, Dict]: A tuple of normalized data with shape
            (T, input_dim) in [0, 1], and a params dict with "min" and "max"
            arrays of shape (input_dim,) describing the per-column scaling.
    """
    if path is not None and os.path.exists(path):
        data = pd.read_csv(path).to_numpy(dtype=np.float64)
    else:
        # Lazy import to avoid a circular import with data_generator.
        from src.models.data_generator import SyntheticDataGenerator

        generator = SyntheticDataGenerator()
        raw = generator.generate_normal_data()
        data = raw.reshape(-1, raw.shape[-1]).astype(np.float64)

    col_min = data.min(axis=0)
    col_max = data.max(axis=0)
    denom = col_max - col_min
    denom[denom == 0] = 1.0

    normalized = (data - col_min) / denom
    params: Dict = {"min": col_min, "max": col_max}

    return normalized, params

def preprocess_sequences(data: np.ndarray, seq_len: int = SEQ_LEN) -> np.ndarray:
    """
    Normalizes input into windowed sequences of shape (N, seq_len, input_dim).

    If the data is already 3D it is returned unchanged. If it is a 2D
    continuous series it is converted into overlapping sliding windows.

    Args:
        data: Either 3D windowed data (N, seq_len, input_dim) or a 2D
            continuous series (T, input_dim).
        seq_len: The length of each temporal window.

    Returns:
        np.ndarray: Windowed sequences of shape (N, seq_len, input_dim).

    Raises:
        ValueError: If a 2D input has fewer rows than seq_len.
    """
    if data.ndim == 3:
        return data

    if data.ndim == 2:
        length = data.shape[0]
        if length < seq_len:
            raise ValueError(f"Data length {length} is less than seq_len {seq_len}")
        return create_sequences(data, seq_len=seq_len)

    raise ValueError(f"Expected 2D or 3D input, but got {data.ndim}D")

def create_data_loaders(
    train_data: np.ndarray,
    batch_size: int = config.BATCH_SIZE,
    train_split: float = 0.8,
) -> Tuple[DataLoader, DataLoader]:
    """
    Splits sequence data into training and validation PyTorch DataLoaders.

    Args:
        train_data: Sequence data of shape (N, seq_len, input_dim).
        batch_size: Number of samples per batch.
        train_split: Proportion of data used for training (0.0 to 1.0).

    Returns:
        Tuple[DataLoader, DataLoader]: A tuple of (train_loader, val_loader).
    """
    train_arr, val_arr = train_val_split(train_data, split_ratio=train_split)

    train_dataset = TensorDataset(to_torch_tensor(train_arr))
    val_dataset = TensorDataset(to_torch_tensor(val_arr))

    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

    return train_loader, val_loader

def sanitize_training_data(data: np.ndarray, contamination: float = 0.05) -> np.ndarray:
    """
    Robust data sanitization using Isolation Forest to remove the most extreme
    multivariate anomalies (e.g. from a dirty dataset) before model training.
    This prevents Cold Start Contamination where the model learns anomalies as 'normal'.

    Args:
        data: The continuous raw sensor data of shape (T, input_dim).
        contamination: The proportion of data to drop (0.0 to 0.5).
                       Set to 0.05 to drop the worst 5%.

    Returns:
        np.ndarray: The sanitized data array.
    """
    if contamination <= 0.0 or data.shape[0] < 50:
        return data

    # Fit an isolation forest to find multivariate outliers
    iso = IsolationForest(contamination=contamination, random_state=42)
    # The predict method returns 1 for inliers and -1 for outliers
    preds = iso.fit_predict(data)
    
    # Keep only the inliers
    sanitized_data = data[preds == 1]
    
    logger = logging.getLogger(__name__)
    logger.info(f"Sanitized training data. Dropped {data.shape[0] - sanitized_data.shape[0]} highly anomalous rows (contamination={contamination}).")
    
    return sanitized_data

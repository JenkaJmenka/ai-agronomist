"""Two-pass cold-start bootstrap: physics-IsolationForest cleaning + LSTM-VAE inference.

Pass 1 (ColdStartBootstrap): flags rows that fail psychrometric plausibility or
IsolationForest outlier detection, saves clean/anomaly CSVs, and returns the
split plus per-row flag series.

Pass 2 (Pass2BootstrapDetector): trains a fresh LSTMVAE on the clean rows and
scores every row in the full (clean + anomaly) DataFrame.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import torch
from sklearn.ensemble import IsolationForest
from torch.utils.data import DataLoader, TensorDataset

from src.config import (
    BOOTSTRAP_GAS_NULL_THRESHOLD,
    BOOTSTRAP_IF_CONTAMINATION,
    BOOTSTRAP_MAX_TRAIN_SEQUENCES,
    BOOTSTRAP_MIN_CLEAN_ROWS,
    BOOTSTRAP_MIN_IF_ROWS,
    BOOTSTRAP_RESAMPLE_INTERVAL,
    BOOTSTRAP_THRESHOLD_PERCENTILE,
    DEVICE,
    SEQ_LEN,
    SPIKE_DELTA_MAX,
)
from src.models.data_utils import (
    SENSOR_RANGES,
    compute_adaptive_sensor_ranges,
    normalize_data,
    preprocess_sequences,
)
from src.models.lstm_vae import LSTMVAE
from src.models.psychrometric_validator import calculate_humidity_deficit

logger = logging.getLogger(__name__)

# Alias so tests that mock Pass 1 with ValueError("Insufficient clean rows…") are
# caught by `except InsufficientDataError` in the route without requiring an import
# of a separate exception class.
InsufficientDataError = ValueError

_SENSOR_COLS: list[str] = ["temperature", "humidity", "co2", "light"]
_GAS_COLS: frozenset[str] = frozenset({"co2", "light"})
_TRAIN_EPOCHS: int = 10
_TRAIN_BATCH: int = 256
_TRAIN_LR: float = 1e-3
_PHYSICS_HD_MAX: float = 50.0  # g/m³ — above this the T+RH combination is impossible


@dataclass
class BootstrapPass1Result:
    """Output of ColdStartBootstrap.run()."""

    clean_df: pd.DataFrame
    anomaly_df: pd.DataFrame
    physics_flags: pd.Series  # bool — True when row failed psychrometric check
    if_flags: pd.Series       # bool — True when IsolationForest marked as outlier
    gas_mask_cols: frozenset  # gas columns dropped due to high null fraction


@dataclass
class BootstrapRowResult:
    """Per-row inference result from Pass2BootstrapDetector.infer()."""

    timestamp: str
    is_anomaly: bool
    reconstruction_loss: float
    pass1_filtered: bool = False             # set by the route after infer()
    root_cause_tags: list[str] = field(default_factory=list)
    contributing_sensors: list[str] = field(default_factory=list)
    physics_validation_status: str = "OK"  # overwritten by route for physics failures


class ColdStartBootstrap:
    """Pass 1: physics check + IsolationForest outlier filtering."""

    def __init__(self, *, clean_path: str, anomaly_path: str) -> None:
        self._clean_path = clean_path
        self._anomaly_path = anomaly_path

    def run(self, csv_path: str) -> BootstrapPass1Result:
        df = _load_and_resample(csv_path)
        physics_flags = _compute_physics_flags(df)
        gas_mask_cols, if_flags = _compute_if_flags(df)

        combined = physics_flags | if_flags
        clean_df = df[~combined]
        anomaly_df = df[combined]

        _persist_csv(clean_df, self._clean_path)
        _persist_csv(anomaly_df, self._anomaly_path)

        if len(clean_df) < BOOTSTRAP_MIN_CLEAN_ROWS:
            raise InsufficientDataError(
                f"Insufficient clean rows: {len(clean_df)} "
                f"(minimum {BOOTSTRAP_MIN_CLEAN_ROWS} required after Pass 1 filtering)"
            )

        return BootstrapPass1Result(
            clean_df=clean_df,
            anomaly_df=anomaly_df,
            physics_flags=physics_flags,
            if_flags=if_flags,
            gas_mask_cols=frozenset(gas_mask_cols),
        )


class Pass2BootstrapDetector:
    """Pass 2: LSTM-VAE trained on Pass 1 clean rows, scoring every row."""

    def __init__(self) -> None:
        self._model: LSTMVAE | None = None
        self._threshold: float = 0.5
        self._ranges: list[tuple[float, float]] = list(SENSOR_RANGES)

    @property
    def trained_model(self) -> LSTMVAE | None:
        return self._model

    def train(self, clean_path: str) -> None:
        df = pd.read_csv(clean_path, index_col=0, parse_dates=True)
        data = _df_to_array(df)
        if len(data) < SEQ_LEN:
            logger.warning("Clean dataset too small for LSTM-VAE training (%d rows)", len(data))
            return

        self._ranges = compute_adaptive_sensor_ranges(data, SENSOR_RANGES)
        norm = normalize_data(data, self._ranges)

        try:
            sequences = preprocess_sequences(norm, seq_len=SEQ_LEN)
        except ValueError:
            logger.warning("preprocess_sequences failed — not enough clean rows")
            return

        sequences = _cap_training_sequences(sequences)

        self._model = LSTMVAE()
        self._model.to(DEVICE)
        train_losses = _train_loop(self._model, sequences)
        if train_losses:
            self._threshold = float(np.percentile(train_losses, BOOTSTRAP_THRESHOLD_PERCENTILE))

    def save_checkpoint(self, path: str) -> float:
        """Persist the trained model; returns the calibration threshold."""
        if self._model is None:
            raise ValueError("No trained model to save")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._model.save_checkpoint(path, epoch=0, loss=self._threshold, accuracy=0.0)
        return self._threshold

    def infer(self, full_df: pd.DataFrame) -> list[BootstrapRowResult]:
        data = _df_to_array(full_df)
        timestamps = _extract_timestamps(full_df)
        n_rows = len(data)

        if self._model is None or n_rows < SEQ_LEN:
            return [
                BootstrapRowResult(timestamp=ts, is_anomaly=False, reconstruction_loss=0.0)
                for ts in timestamps
            ]

        norm = normalize_data(data, self._ranges)
        try:
            sequences = preprocess_sequences(norm, seq_len=SEQ_LEN)
        except ValueError:
            return [
                BootstrapRowResult(timestamp=ts, is_anomaly=False, reconstruction_loss=0.0)
                for ts in timestamps
            ]

        losses = _compute_reconstruction_losses(self._model, sequences)
        results: list[BootstrapRowResult] = []

        # Rows 0…SEQ_LEN-2 have no full covering sequence — mark non-anomaly
        for i in range(min(SEQ_LEN - 1, n_rows)):
            results.append(
                BootstrapRowResult(timestamp=timestamps[i], is_anomaly=False, reconstruction_loss=0.0)
            )

        for seq_idx, loss in enumerate(losses):
            row_idx = seq_idx + SEQ_LEN - 1
            ts = timestamps[row_idx] if row_idx < len(timestamps) else "unknown"
            is_anomaly = loss > self._threshold
            tags, sensors = _classify_root_cause(data, row_idx) if is_anomaly else ([], [])
            results.append(
                BootstrapRowResult(
                    timestamp=ts,
                    is_anomaly=is_anomaly,
                    reconstruction_loss=float(loss),
                    root_cause_tags=tags,
                    contributing_sensors=sensors,
                )
            )

        return results


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _prepare_raw_chunk(chunk: pd.DataFrame) -> pd.DataFrame:
    """Rename pandas index-export first column to timestamp when needed."""
    from src.vendors.csv_adapter import TIMESTAMP_COL

    if chunk.columns.empty:
        return chunk
    first = chunk.columns[0]
    if first == TIMESTAMP_COL:
        return chunk
    if first == "" or str(first).startswith("Unnamed"):
        return chunk.rename(columns={first: TIMESTAMP_COL})
    return chunk


def _load_and_resample(csv_path: str) -> pd.DataFrame:
    from src.vendors.csv_adapter import CSVAdapter, TIMESTAMP_COL, pandas_csv_reader_kwargs
    import random

    adapter = CSVAdapter({"filepath": csv_path})
    adapter.connect()
    csv_props = adapter._csv_props
    
    MAX_CHUNKS = 10
    reservoir: list[pd.DataFrame] = []
    chunk_idx = 0
    total_raw = 0

    try:
        reader = pd.read_csv(
            csv_path,
            chunksize=5000,
            **pandas_csv_reader_kwargs(csv_props),
        )
        for raw_chunk in reader:
            if raw_chunk.empty:
                continue
            
            if chunk_idx < MAX_CHUNKS:
                reservoir.append(raw_chunk)
            else:
                j = random.randint(0, chunk_idx)
                if j < MAX_CHUNKS:
                    reservoir[j] = raw_chunk
            
            chunk_idx += 1
            total_raw += len(raw_chunk)
            
    except pd.errors.EmptyDataError as exc:
        raise ValueError("Uploaded CSV contains no valid data.") from exc

    if not reservoir:
        raise ValueError("Uploaded CSV contains no valid data.")

    # Process and resample each sampled chunk individually
    # This prevents pandas from expanding the time index with millions of NaNs 
    # between chronologically distant sampled chunks.
    pieces: list[pd.DataFrame] = []
    
    # Sort reservoir by time (roughly) so the final dataframe is chronological
    # We can do this safely after processing them
    processed_chunks = []
    
    for raw_chunk in reservoir:
        raw_chunk = _prepare_raw_chunk(raw_chunk)
        try:
            normalized = adapter.normalize(raw_chunk)
            chunk_df = normalized.set_index(TIMESTAMP_COL).sort_index()
            chunk_df.index = pd.to_datetime(chunk_df.index, utc=True)
            chunk_df = chunk_df[~chunk_df.index.duplicated(keep="last")]
            chunk_df = chunk_df.resample(BOOTSTRAP_RESAMPLE_INTERVAL).mean()
            
            for col in _SENSOR_COLS:
                if col not in chunk_df.columns:
                    chunk_df[col] = np.nan
                    
            chunk_df = chunk_df[_SENSOR_COLS].interpolate(method="linear", limit=3)
            processed_chunks.append(chunk_df)
        except Exception:
            continue
            
    if not processed_chunks:
        raise ValueError("Uploaded CSV contains no valid data after normalization.")

    df = pd.concat(processed_chunks).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def _compute_physics_flags(df: pd.DataFrame) -> pd.Series:
    flags = pd.Series(False, index=df.index)
    for ts, row in df.iterrows():
        try:
            t_air = float(row["temperature"])
            rh = float(row["humidity"])
            if pd.isna(t_air) or pd.isna(rh) or rh < 0.0 or rh > 100.0:
                continue
            hd = calculate_humidity_deficit(t_air, rh)
            if abs(hd) > _PHYSICS_HD_MAX:
                flags[ts] = True
        except Exception:
            pass
    return flags


def _compute_if_flags(df: pd.DataFrame) -> tuple[list[str], pd.Series]:
    flags = pd.Series(False, index=df.index)
    feature_cols = list(_SENSOR_COLS)
    gas_mask: list[str] = []

    for col in list(_GAS_COLS):
        if col in df.columns and df[col].isna().mean() > BOOTSTRAP_GAS_NULL_THRESHOLD:
            feature_cols.remove(col)
            gas_mask.append(col)

    sub = df[feature_cols].dropna()
    if len(sub) < BOOTSTRAP_MIN_IF_ROWS:
        return gas_mask, flags

    clf = IsolationForest(contamination=BOOTSTRAP_IF_CONTAMINATION, random_state=42)
    preds = clf.fit_predict(sub.to_numpy())
    flags[sub.index[preds == -1]] = True
    return gas_mask, flags


def _persist_csv(df: pd.DataFrame, path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    df.to_csv(path)


def _df_to_array(df: pd.DataFrame) -> np.ndarray:
    cols = [c for c in _SENSOR_COLS if c in df.columns]
    arr = df[cols].to_numpy(dtype=np.float32)
    if np.isnan(arr).any():
        filled = pd.DataFrame(arr, columns=cols).ffill().fillna(0.0)
        arr = filled.to_numpy(dtype=np.float32)
    return arr


def _extract_timestamps(df: pd.DataFrame) -> list[str]:
    if isinstance(df.index, pd.DatetimeIndex):
        return [
            ts.isoformat() if ts.tzinfo is not None else ts.tz_localize("UTC").isoformat()
            for ts in df.index
        ]
    return [str(idx) for idx in df.index]


def _cap_training_sequences(sequences: np.ndarray) -> np.ndarray:
    """Bound Pass 2 training cost for long-span uploads.

    Training time scales with the sequence count (≈ the resampled row count); an
    uncapped large file exceeded the worker prepare timeout and retrain-looped.
    When over ``BOOTSTRAP_MAX_TRAIN_SEQUENCES`` we take an even-stride subsample,
    which preserves coverage across the whole time range instead of truncating to
    a single window. A cap of 0 disables the behaviour.
    """
    cap = BOOTSTRAP_MAX_TRAIN_SEQUENCES
    total = len(sequences)
    if cap <= 0 or total <= cap:
        return sequences
    stride = total / cap
    indices = (np.arange(cap) * stride).astype(int)
    logger.info(
        "Capping Pass 2 training set from %d to %d sequences (even stride)",
        total,
        cap,
    )
    return sequences[indices]


def _train_loop(model: LSTMVAE, sequences: np.ndarray) -> list[float]:
    tensor = torch.from_numpy(sequences).float().to(DEVICE)
    loader = DataLoader(TensorDataset(tensor), batch_size=_TRAIN_BATCH, shuffle=True)
    optimizer = torch.optim.Adam(model.parameters(), lr=_TRAIN_LR)
    model.train()

    for _ in range(_TRAIN_EPOCHS):
        for (batch,) in loader:
            optimizer.zero_grad()
            recon, mu, logvar, _ = model(batch)
            loss = model.loss_function(recon, batch, mu, logvar)
            loss.backward()
            optimizer.step()

    model.eval()
    calibration_losses: list[float] = []
    with torch.no_grad():
        for i in range(0, len(tensor), _TRAIN_BATCH):
            batch = tensor[i : i + _TRAIN_BATCH]
            recon, _, _, _ = model(batch)
            per_seq = torch.mean((batch - recon) ** 2, dim=[1, 2])
            calibration_losses.extend(per_seq.cpu().tolist())

    return calibration_losses


def _compute_reconstruction_losses(model: LSTMVAE, sequences: np.ndarray) -> list[float]:
    tensor = torch.from_numpy(sequences).float().to(DEVICE)
    model.eval()
    losses: list[float] = []
    with torch.no_grad():
        for i in range(0, len(tensor), _TRAIN_BATCH):
            batch = tensor[i : i + _TRAIN_BATCH]
            recon, _, _, _ = model(batch)
            per_seq = torch.mean((batch - recon) ** 2, dim=[1, 2])
            losses.extend(per_seq.cpu().tolist())
    return losses


def _classify_root_cause(data: np.ndarray, row_idx: int) -> tuple[list[str], list[str]]:
    """Heuristic root cause tagger: spike, stuck, or dropout per sensor."""
    tags: list[str] = []
    sensors: list[str] = []
    start = max(0, row_idx - SEQ_LEN + 1)
    window = data[start : row_idx + 1]

    for i, col in enumerate(_SENSOR_COLS):
        col_vals = window[:, i] if i < window.shape[1] else np.array([])
        if len(col_vals) == 0 or np.isnan(col_vals).all():
            tags.append("dropout")
            sensors.append(col)
            continue
        valid = col_vals[~np.isnan(col_vals)]
        if len(valid) < 2:
            continue
        velocity = float(np.max(np.abs(np.diff(valid))))
        spike_limit = SPIKE_DELTA_MAX.get(col, float("inf"))
        if velocity > spike_limit:
            if "spike" not in tags:
                tags.append("spike")
            sensors.append(col)
        elif float(np.std(valid)) < 1e-6:
            if "stuck" not in tags:
                tags.append("stuck")
            sensors.append(col)

    return tags, sensors

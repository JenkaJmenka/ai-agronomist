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
    BOOTSTRAP_LOADER_CHUNK_SIZE,
    BOOTSTRAP_LOADER_RANDOM_SEED,
    BOOTSTRAP_MAX_TRAIN_SEQUENCES,
    BOOTSTRAP_MIN_CLEAN_ROWS,
    BOOTSTRAP_MIN_IF_ROWS,
    BOOTSTRAP_PHYSICS_HD_MAX,
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
from src.models.mad_scrub import compute_mad_scrub
from src.models.mad_scrub_report import MadChannelReport
from src.models.pass1_discard_guard import (
    DISCARD_STATUS_FAIL,
    ExcessivePass1DiscardError,
    Pass1DiscardStats,
    evaluate_pass1_discard,
)
from src.models.pass1_warning import Pass1Warning
from src.models.psychrometric_validator import calculate_humidity_deficit
from src.utils.profiling import timed

logger = logging.getLogger(__name__)

# Alias so tests that mock Pass 1 with ValueError("Insufficient clean rows…") are
# caught by `except InsufficientDataError` in the route without requiring an import
# of a separate exception class.
InsufficientDataError = ValueError

# Pass 1's second failure mode, ExcessivePass1DiscardError, is imported above
# and so is also reachable as src.models.bootstrap.ExcessivePass1DiscardError.
# It subclasses ValueError, i.e. InsufficientDataError, so the route's existing
# `except InsufficientDataError` still maps it to HTTP 400.

# The canonical channel set, and the reason BOOTSTRAP_MAD_EXCLUDED_CHANNELS is
# bounded rather than an open special-case register: that list is keyed on these
# names, so it can never hold more than these four entries, and the default
# holds one (`light`). If you ADD a channel here, the MAD scrub will evaluate it
# with a whole-file median/MAD — check it is unimodal first, or the BACKLOG-14
# failure recurs for the new channel. If you REMOVE one, drop it from
# BOOTSTRAP_MAD_EXCLUDED_CHANNELS too, or the config carries a name that
# matches nothing and the scrub silently stops excluding anything.
_SENSOR_COLS: list[str] = ["temperature", "humidity", "co2", "light"]
_GAS_COLS: frozenset[str] = frozenset({"co2", "light"})
_TRAIN_EPOCHS: int = 10
_TRAIN_BATCH: int = 256
_TRAIN_LR: float = 1e-3
# _PHYSICS_HD_MAX is now BOOTSTRAP_PHYSICS_HD_MAX in src.config (env-overridable).


@dataclass
class BootstrapPass1Result:
    """Output of ColdStartBootstrap.run().

    The observability fields are documented as a cross-agent contract in
    ``docs/handoff/backlog14_pass1_observability.md``. Every fraction on this
    class is a fraction in [0, 1] — never a percentage.
    """

    clean_df: pd.DataFrame
    anomaly_df: pd.DataFrame
    physics_flags: pd.Series  # bool — True when row failed psychrometric check
    mad_flags: pd.Series      # bool — True when MAD robust Z cull flagged the row
    if_flags: pd.Series       # bool — True when IsolationForest marked as outlier
    gas_mask_cols: frozenset  # gas columns dropped due to high null fraction
    # Observability (BACKLOG-14). Defaulted so existing constructors still work.
    mad_channels: tuple[MadChannelReport, ...] = ()
    if_flag_fraction: float = 0.0
    discard_stats: Pass1DiscardStats = field(default_factory=Pass1DiscardStats)
    warnings: tuple[Pass1Warning, ...] = ()

    @property
    def mad_channel_cull_fractions(self) -> dict[str, float]:
        """Channel → MAD cull fraction of that channel's finite values."""
        return {report.column: report.cull_fraction for report in self.mad_channels}

    @property
    def mad_channel_cull_rows(self) -> dict[str, int]:
        """Channel → absolute MAD cull count."""
        return {report.column: report.cull_rows for report in self.mad_channels}

    @property
    def mad_channel_finite_rows(self) -> dict[str, int]:
        """Channel → finite-value count, the cull-fraction denominator."""
        return {report.column: report.finite_rows for report in self.mad_channels}

    @property
    def mad_skipped_channels(self) -> dict[str, str]:
        """Channel → reason, for channels the MAD scrub did not evaluate."""
        return {
            report.column: report.skip_reason
            for report in self.mad_channels
            if report.is_skipped and report.skip_reason is not None
        }


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
    """Pass 1: physics + MAD scrub + IsolationForest outlier filtering."""

    def __init__(self, *, clean_path: str, anomaly_path: str) -> None:
        self._clean_path = clean_path
        self._anomaly_path = anomaly_path

    def run(self, csv_path: str) -> BootstrapPass1Result:
        with timed(logger, "bootstrap.pass1.load_resample") as ctx:
            df = _load_and_resample(csv_path)
            ctx["rows"] = len(df)
        with timed(logger, "bootstrap.pass1.physics_flags", rows=len(df)):
            physics_flags = _compute_physics_flags(df)
        with timed(logger, "bootstrap.pass1.mad_scrub", rows=len(df)):
            mad_scrub = compute_mad_scrub(df, _SENSOR_COLS)
            mad_flags = mad_scrub.flags
        with timed(logger, "bootstrap.pass1.isolation_forest", rows=len(df)):
            gas_mask_cols, if_flags = _compute_if_flags(df)

        combined = physics_flags | mad_flags | if_flags
        clean_df = df[~combined]
        anomaly_df = df[combined]

        _persist_csv(clean_df, self._clean_path)
        _persist_csv(anomaly_df, self._anomaly_path)

        if len(clean_df) < BOOTSTRAP_MIN_CLEAN_ROWS:
            raise InsufficientDataError(
                f"Insufficient clean rows: {len(clean_df)} "
                f"(minimum {BOOTSTRAP_MIN_CLEAN_ROWS} required after Pass 1 filtering)"
            )

        discard_stats = evaluate_pass1_discard(df, combined, _SENSOR_COLS)
        warnings = mad_scrub.warnings + discard_stats.warnings
        # Each producer already logged its own engineering detail, including the
        # env-var remediation that must not reach the tenant; log the codes here
        # so one line ties a run to the warnings it returned.
        if warnings:
            logger.warning(
                "Pass 1 returned %d warning(s): %s",
                len(warnings),
                [warning.code for warning in warnings],
            )
        if discard_stats.status == DISCARD_STATUS_FAIL:
            # str(exc) becomes the HTTP 400 body, so this must be the
            # tenant-safe message, never the code and never the log text.
            raise ExcessivePass1DiscardError(discard_stats.warnings[0].message)

        return BootstrapPass1Result(
            clean_df=clean_df,
            anomaly_df=anomaly_df,
            physics_flags=physics_flags,
            mad_flags=mad_flags,
            if_flags=if_flags,
            gas_mask_cols=frozenset(gas_mask_cols),
            mad_channels=mad_scrub.channels,
            if_flag_fraction=float(if_flags.mean()) if len(if_flags) else 0.0,
            discard_stats=discard_stats,
            warnings=warnings,
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
        with timed(
            logger,
            "bootstrap.pass2.train",
            sequences=len(sequences),
            epochs=_TRAIN_EPOCHS,
            device=DEVICE,
        ):
            train_losses = _train_loop(self._model, sequences)
        if train_losses:
            self._threshold = float(np.percentile(train_losses, BOOTSTRAP_THRESHOLD_PERCENTILE))

    @classmethod
    def from_checkpoint(
        cls,
        path: str,
        *,
        ranges: list[tuple[float, float]] | None = None,
    ) -> "Pass2BootstrapDetector":
        """Rebuild a scorer from a saved checkpoint, for offline comparison.

        Added for the shadow-retraining procedure in
        ``docs/runbooks/cold-start-retraining.md``, which has to score one
        window with an old and a new model. It exists so that tooling does not
        reach into ``_model``/``_threshold``.

        **The checkpoint does not carry the training-time sensor ranges.**
        ``train`` derives adaptive ranges via ``compute_adaptive_sensor_ranges``
        and keeps them only in memory; ``save_checkpoint`` passes no
        ``model_config``. So a reloaded detector normalizes with whatever
        *ranges* is given, defaulting to the static ``SENSOR_RANGES`` — which is
        also what the live inference path uses, so the default reproduces
        production rather than the original training. Pass *ranges* explicitly
        when comparing two models, so both sides normalize alike; a comparison
        that lets the two differ is measuring the normalization, not the models.

        The calibration threshold *is* carried, in the checkpoint's ``loss``
        field, which is where ``save_checkpoint`` puts it.
        """
        detector = cls()
        model = LSTMVAE()
        model.to(DEVICE)
        checkpoint = model.load_checkpoint(path)
        model.eval()
        detector._model = model
        detector._threshold = float(checkpoint.get("loss", detector._threshold))
        if ranges is not None:
            detector._ranges = list(ranges)
        return detector

    @property
    def calibration_threshold(self) -> float:
        """The anomaly cutoff this detector scores against."""
        return self._threshold

    @property
    def sensor_ranges(self) -> list[tuple[float, float]]:
        """The normalization ranges in use — adaptive after ``train``."""
        return list(self._ranges)

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
    """Stream a CSV file and return a clean DataFrame with ALL rows preserved.

    Every chunk is processed (no reservoir sampling / row cap).  Memory stays
    bounded because only one chunk is in memory at a time before being appended
    to the result list.

    Real measurements are never replaced: ``.resample().mean()`` and
    ``.interpolate()`` are NOT applied to rows that already carry values.
    Duplicate timestamps are deduplicated (keep last).  Missing sensor columns
    are added as NaN.  Original nulls are preserved so downstream consumers
    can distinguish real dropouts from derived values.
    """
    from src.vendors.csv_adapter import CSVAdapter, TIMESTAMP_COL, pandas_csv_reader_kwargs

    adapter = CSVAdapter({"filepath": csv_path})
    adapter.connect()
    csv_props = adapter._csv_props

    processed_chunks: list[pd.DataFrame] = []
    chunk_idx = 0
    total_raw = 0
    failed_chunks = 0

    try:
        reader = pd.read_csv(
            csv_path,
            chunksize=BOOTSTRAP_LOADER_CHUNK_SIZE,
            **pandas_csv_reader_kwargs(csv_props),
        )
        for raw_chunk in reader:
            if raw_chunk.empty:
                chunk_idx += 1
                continue

            total_raw += len(raw_chunk)
            raw_chunk = _prepare_raw_chunk(raw_chunk)

            try:
                normalized = adapter.normalize(raw_chunk)
                chunk_df = normalized.set_index(TIMESTAMP_COL).sort_index()
                chunk_df.index = pd.to_datetime(chunk_df.index, utc=True)
                chunk_df = chunk_df[~chunk_df.index.duplicated(keep="last")]

                for col in _SENSOR_COLS:
                    if col not in chunk_df.columns:
                        chunk_df[col] = np.nan

                chunk_df = chunk_df[_SENSOR_COLS]
                processed_chunks.append(chunk_df)
            except Exception as exc:
                first_row = chunk_idx * BOOTSTRAP_LOADER_CHUNK_SIZE
                last_row = first_row + len(raw_chunk) - 1
                logger.warning(
                    "Bootstrap loader: skipping chunk %d (rows %d–%d): %s",
                    chunk_idx,
                    first_row,
                    last_row,
                    exc,
                )
                failed_chunks += 1

            chunk_idx += 1

    except pd.errors.EmptyDataError as exc:
        raise ValueError("Uploaded CSV contains no valid data.") from exc

    if not processed_chunks:
        raise ValueError("Uploaded CSV contains no valid data after normalization.")

    if failed_chunks:
        logger.info(
            "Bootstrap loader: %d of %d chunks failed normalization",
            failed_chunks,
            chunk_idx,
        )

    df = pd.concat(processed_chunks).sort_index()
    df = df[~df.index.duplicated(keep="last")]

    logger.info(
        "Bootstrap loader: %d raw rows → %d output rows (%.1f%% retained)",
        total_raw,
        len(df),
        100.0 * len(df) / total_raw if total_raw else 0.0,
    )
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
            if abs(hd) > BOOTSTRAP_PHYSICS_HD_MAX:
                flags[ts] = True
        except Exception:
            pass
    return flags


def _compute_if_flags(df: pd.DataFrame) -> tuple[list[str], pd.Series]:
    """Flag IsolationForest outliers.

    **The invariant is `fit population == predict population`.** It is not a
    prohibition on removing rows: removing rows is fine, and a future stage may
    well want to — a flatline/stuck rule (BACKLOG-21) would need exactly that.
    What is forbidden is the *asymmetry*, fitting on one population and scoring
    a different one. If you drop rows, drop them from both sides.

    Why: ``contamination`` calibrates the decision boundary to the distribution
    the forest was fitted on, so scoring a different distribution voids the
    calibration silently. Between 2026-07-24 (``892bcd3``) and 2026-11-08 the
    fit excluded MAD-flagged rows while ``.predict()`` ran over the whole frame,
    which turned IsolationForest from a detector into an amplifier of the MAD
    cull: on the tenant_1 corpus it flagged 4.00% of its fit subset — the
    contamination it was asked for — but 98.08% of the rows MAD had already
    culled, so a MAD misfire arrived at the operator as two independent
    detectors agreeing.

    The removed code credited that split to a spec ("Spec: MAD-flagged rows are
    excluded from IF *fit*, then OR-ed in."). No such spec item exists; see
    ``docs/handoff/backlog14_pass1_observability.md`` §3.
    """
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

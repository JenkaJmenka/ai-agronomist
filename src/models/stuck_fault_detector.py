"""
Stuck-fault detector for greenhouse sensors.

Two detection modes:

1. **Sample-indexed** (``StuckFaultDetector``) — rolling-variance analysis
   over fixed-length windows.  Used by ``ComprehensiveInferencePipeline``.

2. **Wall-clock** (``WallclockStuckDetector``) — elapsed-time gate with a
   per-channel epsilon.  Used by the CSV upload path
   (``score_portion`` / ``finalize_scores``).  Correctly handles irregular
   cadence and NaN gaps inside a fault window.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Set

import numpy as np
import pandas as pd

from src.config import (
    CADENCE_EXPECTED_SECONDS,
    CADENCE_TOLERANCE_FACTOR,
    SENSOR_NAMES,
    STUCK_FLAT_RANGE_THRESHOLD,
    STUCK_WALLCLOCK_CHANNELS,
    STUCK_WALLCLOCK_EPSILON,
    STUCK_WALLCLOCK_MIN_HOURS,
    STUCK_WALLCLOCK_MIN_SAMPLES,
)

logger = logging.getLogger(__name__)

try:
    from src.config import STUCK_EPSILON, STUCK_WINDOW_K, STUCK_VARIANCE_THRESHOLD
except ImportError:  # pragma: no cover
    STUCK_WINDOW_K = 10
    STUCK_VARIANCE_THRESHOLD = 1e-4
    STUCK_EPSILON = 1e-4

_SENSOR_NAMES: list[str] = list(SENSOR_NAMES)


class StuckFaultDetector:
    """Detect sensors stuck at a constant value via rolling variance analysis."""

    def __init__(
        self,
        variance_threshold: float = STUCK_VARIANCE_THRESHOLD,
        window_k: int = STUCK_WINDOW_K,
        flat_range_threshold: float = STUCK_FLAT_RANGE_THRESHOLD,
    ) -> None:
        self._variance_threshold = variance_threshold
        self._window_k = window_k
        self._flat_range_threshold = flat_range_threshold

    def detect(
        self,
        sequence: np.ndarray,
        skip_channels: Optional[Set[str]] = None,
    ) -> dict[str, bool]:
        """Check each sensor channel for stuck-fault condition."""
        if sequence.ndim != 2:
            raise ValueError(
                f"Expected 2D array (seq_len, sensors), got {sequence.ndim}D"
            )

        skip = skip_channels or set()
        num_sensors = min(sequence.shape[1], len(_SENSOR_NAMES))
        results: dict[str, bool] = {}

        seq_active = sequence[:, :num_sensors]
        if sequence.shape[0] >= self._window_k:
            windows = np.lib.stride_tricks.sliding_window_view(seq_active, self._window_k, axis=0)
            variances = np.var(windows, axis=-1, ddof=1)
            min_variances = np.min(variances, axis=0)
        else:
            min_variances = np.var(seq_active, axis=0)

        # Standardize NaNs to 1.0
        min_variances = np.where(np.isnan(min_variances), 1.0, min_variances)

        value_ranges = np.max(seq_active, axis=0) - np.min(seq_active, axis=0)
        is_flats = value_ranges < self._flat_range_threshold

        for idx in range(num_sensors):
            name = _SENSOR_NAMES[idx]
            if name in skip:
                results[name] = False
                continue
            is_flat = bool(is_flats[idx])
            variance = float(min_variances[idx])
            results[name] = bool(
                is_flat and variance < self._variance_threshold
            )
        return results

    @staticmethod
    def detect_series(
        series: np.ndarray,
        w: int = STUCK_WINDOW_K,
        epsilon: float = STUCK_EPSILON,
    ) -> list[bool]:
        """Per-timestep stuck-at detection on a 1-D time series.

        Uses rolling variance with Bessel's correction (``ddof=1``).

        Args:
            series: 1-D array of sensor readings.
            w: Rolling window size.
            epsilon: Minimum variance threshold.

        Returns:
            List of booleans; ``True`` when the rolling variance
            falls below *epsilon*.  The first ``w-1`` entries are
            always ``False`` (insufficient window).
        """
        series = np.asarray(series, dtype=float)
        n = len(series)
        flags: list[bool] = [False] * n

        if n < w or w < 2:
            return flags

        for t in range(w - 1, n):
            window = series[t - w + 1 : t + 1]
            var = float(np.var(window, ddof=1))
            flags[t] = var < epsilon

        return flags


# =========================================================================
# Wall-clock stuck-fault detection (upload path)
# =========================================================================

def _parse_timestamp(ts_val: object) -> datetime | None:
    """Parse a timestamp value into a timezone-aware UTC datetime."""
    if isinstance(ts_val, pd.Timestamp):
        dt = ts_val.to_pydatetime()
    elif isinstance(ts_val, datetime):
        dt = ts_val
    elif isinstance(ts_val, str):
        try:
            dt = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class ChannelFreezeState:
    """Per-channel frozen-value tracking for wall-clock stuck detection."""

    frozen_value: float
    onset_ts: str
    onset_dt: datetime
    last_ts: str
    last_dt: datetime
    sample_count: int


@dataclass
class FaultWindow:
    """Recorded fault period for post-fault baseline cleanup.

    ``end_ts`` / ``end_dt`` point to the first **non-frozen** sample
    (the release point) — used for recovery-window and baseline-purge
    calculations.

    ``last_frozen_ts`` / ``last_frozen_dt`` point to the last sample
    that was still at the frozen value — used for accurate frozen-span
    duration (onset → last frozen sample).
    """

    channel: str
    onset_ts: str
    onset_dt: datetime
    end_ts: str
    end_dt: datetime
    last_frozen_ts: str | None = None
    last_frozen_dt: datetime | None = None


@dataclass
class StuckCarry:
    """Rolling state for wall-clock stuck-fault detection across portions.

    Bounded memory: stores one ``ChannelFreezeState`` per monitored channel
    (constant-size) plus a short list of per-chunk cadence medians.

    ``skip_channels`` excludes imputed channels (constant fill values that
    would otherwise trigger a false stuck alert for the entire file).

    ``recovery_channels`` maps channel → FaultWindow for channels that just
    exited a detected fault.  Used by the statistical detector to purge
    contaminated baseline values and by episode grouping to tag recovery
    alerts.
    """

    channels: dict[str, ChannelFreezeState] = field(default_factory=dict)
    chunk_medians: list[float] = field(default_factory=list)
    last_timestamp_dt: datetime | None = None
    is_cadence_flagged: bool = False
    cadence_median_seconds: float | None = None
    emitted_onsets: set[tuple[str, str]] = field(default_factory=set)
    skip_channels: frozenset[str] = field(default_factory=frozenset)
    recovery_channels: dict[str, FaultWindow] = field(default_factory=dict)
    completed_faults: list[FaultWindow] = field(default_factory=list)


class WallclockStuckDetector:
    """Wall-clock-gated stuck-fault detection for the CSV upload path.

    Unlike ``StuckFaultDetector`` (sample-indexed), this gates on elapsed
    time so it works correctly regardless of the input's sampling cadence.
    A freeze must persist for at least ``min_hours`` AND have at least
    ``min_samples`` identical readings.

    Usage (streaming, chunk-by-chunk)::

        carry = StuckCarry()
        det = WallclockStuckDetector()
        for chunk_df in chunks:
            alerts = det.detect_chunk(chunk_df, carry)
        final_alerts = det.flush(carry)
    """

    def __init__(
        self,
        min_hours: float = STUCK_WALLCLOCK_MIN_HOURS,
        min_samples: int = STUCK_WALLCLOCK_MIN_SAMPLES,
        channel_epsilon: dict[str, float] | None = None,
        channels: tuple[str, ...] = STUCK_WALLCLOCK_CHANNELS,
    ) -> None:
        self._min_hours = min_hours
        self._min_samples = min_samples
        self._epsilon = channel_epsilon or dict(STUCK_WALLCLOCK_EPSILON)
        self._channels = channels

    # -- public interface --------------------------------------------------

    def detect_chunk(
        self,
        normalized_df: pd.DataFrame,
        carry: StuckCarry,
    ) -> list[dict[str, Any]]:
        """Process one chunk, returning alerts for freezes that ended.

        Each alert is a dict with keys: ``timestamp``, ``channel``,
        ``severity``, ``detector``, ``value``, ``duration_hours``,
        ``method``.
        """
        alerts: list[dict[str, Any]] = []

        if normalized_df.empty or "timestamp" not in normalized_df.columns:
            return alerts

        timestamps = normalized_df["timestamp"]
        intervals: list[float] = []

        for row_idx in range(len(normalized_df)):
            ts_raw = timestamps.iloc[row_idx]
            if pd.isnull(ts_raw):
                continue

            row_dt = _parse_timestamp(ts_raw)
            if row_dt is None:
                continue

            # --- cadence tracking ---
            if carry.last_timestamp_dt is not None:
                delta = (row_dt - carry.last_timestamp_dt).total_seconds()
                if delta > 0:
                    intervals.append(delta)
            carry.last_timestamp_dt = row_dt

            ts_iso = row_dt.isoformat()

            # --- per-channel freeze tracking ---
            for channel in self._channels:
                if channel not in normalized_df.columns:
                    continue
                if channel in carry.skip_channels:
                    continue

                value = normalized_df[channel].iloc[row_idx]
                if pd.isnull(value) or not np.isfinite(float(value)):
                    continue  # NaN → skip, don't break or extend

                value_f = float(value)
                epsilon = self._epsilon.get(channel, 0.01)
                state = carry.channels.get(channel)

                if state is None:
                    carry.channels[channel] = ChannelFreezeState(
                        frozen_value=value_f,
                        onset_ts=ts_iso,
                        onset_dt=row_dt,
                        last_ts=ts_iso,
                        last_dt=row_dt,
                        sample_count=1,
                    )
                elif abs(value_f - state.frozen_value) <= epsilon:
                    state.last_ts = ts_iso
                    state.last_dt = row_dt
                    state.sample_count += 1
                    # Task 2b: evaluate gate DURING the freeze so
                    # the alert fires as soon as the gate is met,
                    # not only when the freeze ends.  emitted_onsets
                    # prevents duplicate emission on later rows.
                    alert = self._check_gate(
                        channel, state, carry, detected_ts=ts_iso,
                    )
                    if alert is not None:
                        alerts.append(alert)
                else:
                    # Value changed → freeze ended.  Try to emit if
                    # not already emitted mid-freeze.
                    alert = self._check_gate(
                        channel, state, carry, detected_ts=ts_iso,
                    )
                    if alert is not None:
                        alerts.append(alert)
                    # Record recovery for baseline cleanup (Task 3).
                    # Only if a fault was actually detected for this
                    # freeze (key is in emitted_onsets).
                    key = (channel, state.onset_ts)
                    if key in carry.emitted_onsets:
                        fw = FaultWindow(
                            channel=channel,
                            onset_ts=state.onset_ts,
                            onset_dt=state.onset_dt,
                            end_ts=ts_iso,
                            end_dt=row_dt,
                            last_frozen_ts=state.last_ts,
                            last_frozen_dt=state.last_dt,
                        )
                        carry.recovery_channels[channel] = fw
                        carry.completed_faults.append(fw)
                    carry.channels[channel] = ChannelFreezeState(
                        frozen_value=value_f,
                        onset_ts=ts_iso,
                        onset_dt=row_dt,
                        last_ts=ts_iso,
                        last_dt=row_dt,
                        sample_count=1,
                    )

        if intervals:
            carry.chunk_medians.append(float(np.median(intervals)))

        return alerts

    def flush(self, carry: StuckCarry) -> list[dict[str, Any]]:
        """Flush still-open freezes and finalise cadence check."""
        alerts: list[dict[str, Any]] = []
        for channel, state in carry.channels.items():
            alert = self._check_gate(
                channel, state, carry, detected_ts=state.last_ts,
            )
            if alert is not None:
                alerts.append(alert)

        # --- cadence check ---
        if carry.chunk_medians:
            overall_median = float(np.median(carry.chunk_medians))
            carry.cadence_median_seconds = overall_median
            ratio = overall_median / CADENCE_EXPECTED_SECONDS
            if (
                ratio > CADENCE_TOLERANCE_FACTOR
                or ratio < (1.0 / CADENCE_TOLERANCE_FACTOR)
            ):
                carry.is_cadence_flagged = True
                logger.warning(
                    "Cadence deviation: median interval %.1fs vs expected %ds "
                    "(ratio %.2f, tolerance %.1f)",
                    overall_median,
                    CADENCE_EXPECTED_SECONDS,
                    ratio,
                    CADENCE_TOLERANCE_FACTOR,
                )

        return alerts

    # -- internal ----------------------------------------------------------

    def _check_gate(
        self,
        channel: str,
        state: ChannelFreezeState,
        carry: StuckCarry,
        *,
        detected_ts: str | None = None,
    ) -> dict[str, Any] | None:
        """Emit an alert dict if the freeze meets the gate.

        ``detected_ts`` is the row timestamp at which the gate was first
        satisfied — distinct from ``state.onset_ts`` (when the freeze
        began).  The alert carries both so downstream code can compute
        true detection latency as ``detected_ts - onset_ts``.

        ``duration_hours`` is elapsed-so-far at detection time (the freeze
        may still be open).  Episode grouping updates the final duration
        when the freeze ends.
        """
        duration_hours = (
            (state.last_dt - state.onset_dt).total_seconds() / 3600.0
        )
        if duration_hours < self._min_hours:
            return None
        if state.sample_count < self._min_samples:
            return None

        key = (channel, state.onset_ts)
        if key in carry.emitted_onsets:
            return None
        carry.emitted_onsets.add(key)

        return {
            "timestamp": state.onset_ts,
            "detected_ts": detected_ts or state.last_ts,
            "channel": channel,
            "severity": "urgent",
            "detector": "stuck_fault",
            "value": state.frozen_value,
            "duration_hours": round(duration_hours, 2),
            "method": "wallclock_stuck",
        }

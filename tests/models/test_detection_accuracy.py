"""Phase 1 accuracy gate. Bar is 90% composite, not a number to tune toward.

Composite = 0.5 * clean_specificity + 0.5 * mean detection recall on three
injected faults: a +6 °C spike on the last timestep, temperature noise
σ = 1 °C, and a two-channel level shift. Label correctness is recorded
and is not part of the bar.

Clean specificity is the held-out half of ``native_july_clearsky`` (stride
6, sequence 20). Noise is calibrated on the first half, in physical units.
The fixture has no CO2 column, so the adapter's constant fill is replaced
with a plausible trace before the split. Each window is one enrichment
call: ``normalize_data`` plus a fresh pipeline, with ``noise_clean_windows``
set. ``datetime.now`` inside the pipeline is pinned to the window's end
time because physics reads the clock, and on the live path that clock is
the sample time.

Upload spikes go through ``_detect_upload_mad_spikes`` (the helper
``run_cold_run_upload`` already calls). The cold-run entry point also
runs the LSTM on every row, which this gate does not need.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services import comprehensive_pipeline as pipeline_module
from src.api.services.comprehensive_pipeline import ComprehensiveInferencePipeline
from src.api.services.upload_portions import (
    _detect_upload_mad_spikes,
    _timestamp_label,
)
from src.config import SENSOR_NAMES, SEQ_LEN
from src.db.models import Base
from src.models.data_utils import SENSOR_RANGES, normalize_data
from src.models.inference import AnomalyDetector
from src.vendors.csv_adapter import CSVAdapter

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_JULY = _FIXTURES / "native_july_clearsky.csv"
_CHECKPOINT = _FIXTURES / "epoch_0072_trained_baseline.pt"
_MEASUREMENTS = (
    Path(__file__).resolve().parents[2] / "tasks" / "accuracy_gate_measurements.json"
)

_STRIDE = 6
_N_TRIALS = 40
_COMPOSITE_BAR = 0.90
_UPLOAD_RECALL_BAR = 0.95
_SPIKE_DELTA_C = 6.0
_NOISE_SIGMA_C = 1.0
_LEVEL_SHIFT_START = 8
_LEVEL_SHIFT_TEMP_C = 3.0
_LEVEL_SHIFT_HUMIDITY = -10.0
_UPLOAD_SPIKE_COUNT = 12
_UPLOAD_SPIKE_SEED = 11

_EXPECTED_LABEL = {
    "spike": "spike_fault",
    "noise": "accuracy_decline",
    "level_shift": "calibration_drift",
}

_NOON = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)


class _PinnedDatetime(datetime):
    """Stand-in for ``datetime.now`` so time-of-day rules see the sample hour."""

    pinned: datetime | None = None

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        pinned = cls.pinned
        if pinned is None:
            return datetime.now(tz)
        if tz is not None and pinned.tzinfo is None:
            return pinned.replace(tzinfo=tz)
        return pinned


def _record(section: str, payload: dict) -> None:
    current: dict = {}
    if _MEASUREMENTS.exists():
        current = json.loads(_MEASUREMENTS.read_text())
    current[section] = payload
    _MEASUREMENTS.parent.mkdir(parents=True, exist_ok=True)
    _MEASUREMENTS.write_text(json.dumps(current, indent=2) + "\n")


def _as_utc(value) -> datetime:
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _load_july() -> pd.DataFrame:
    adapter = CSVAdapter({"filepath": str(_JULY)})
    adapter.connect()
    try:
        return adapter.normalize(adapter.read())
    finally:
        adapter.disconnect()


def _plausible_co2(n_rows: int) -> np.ndarray:
    """Slow wander plus 3 ppm sensor noise. Not the imputed constant 400."""
    rng = np.random.default_rng(7)
    step = np.arange(n_rows)
    wander = 25.0 * np.sin(2.0 * np.pi * step / 288.0)
    return 410.0 + wander + rng.normal(0.0, 3.0, n_rows)


def _windows(frame: pd.DataFrame) -> list[tuple[np.ndarray, datetime]]:
    values = frame.loc[:, SENSOR_NAMES].to_numpy(dtype=float)
    timestamps = frame["timestamp"].tolist()
    limit = len(values) - SEQ_LEN + 1
    windows: list[tuple[np.ndarray, datetime]] = []
    for start in range(0, limit, _STRIDE):
        end = start + SEQ_LEN
        windows.append((values[start:end].copy(), _as_utc(timestamps[end - 1])))
    return windows


def _ideal_window(seed: int) -> np.ndarray:
    """Quiet in-range window: 24 °C / 70 % / 400 ppm / 500 W/m²."""
    rng = np.random.default_rng(seed)
    step = np.arange(SEQ_LEN)
    temperature = 24.0 + 0.15 * np.sin(2.0 * np.pi * step / 40.0)
    temperature = temperature + rng.normal(0.0, 0.03, SEQ_LEN)
    humidity = 70.0 - 0.4 * np.sin(2.0 * np.pi * step / 40.0)
    humidity = humidity + rng.normal(0.0, 0.08, SEQ_LEN)
    co2 = 400.0 + rng.normal(0.0, 2.0, SEQ_LEN)
    light = 500.0 + 10.0 * np.sin(2.0 * np.pi * step / 60.0)
    light = light + rng.normal(0.0, 1.5, SEQ_LEN)
    return np.column_stack([temperature, humidity, co2, light])


def _inject(kind: str, base: np.ndarray, seed: int) -> np.ndarray:
    window = base.copy()
    if kind == "spike":
        window[-1, 0] += _SPIKE_DELTA_C
        return window
    if kind == "noise":
        noise = np.random.default_rng(1000 + seed).normal(0.0, _NOISE_SIGMA_C, SEQ_LEN)
        window[:, 0] += noise
        return window
    if kind == "level_shift":
        window[_LEVEL_SHIFT_START:, 0] += _LEVEL_SHIFT_TEMP_C
        window[_LEVEL_SHIFT_START:, 1] += _LEVEL_SHIFT_HUMIDITY
        return window
    raise ValueError(kind)


def _rate(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def _fault_counts(labels: list) -> dict[str, int]:
    return dict(Counter(str(label) for label in labels))


async def _score_window(
    session: AsyncSession,
    detector: AnomalyDetector,
    window: np.ndarray,
    end_ts: datetime,
    tenant: str,
    clean_windows: list[np.ndarray],
) -> tuple[bool, str | None]:
    _PinnedDatetime.pinned = end_ts
    normalized = normalize_data(window, SENSOR_RANGES)
    pipeline = ComprehensiveInferencePipeline(detector=detector)
    result = await pipeline.run(
        customer_id=tenant,
        sequence=normalized.tolist(),
        db=session,
        greenhouse_id="default",
        sequence_timestamp=end_ts.isoformat(),
        noise_clean_windows=clean_windows,
    )
    fault = result.get("fault_type")
    return bool(result["is_anomaly"]), None if fault is None else str(fault)


async def _measure_pipeline() -> dict:
    frame = _load_july()
    co2 = frame["co2"].to_numpy(dtype=float)
    co2_span = float(np.ptp(co2))
    if co2_span <= 1e-6:
        frame = frame.copy()
        frame["co2"] = _plausible_co2(len(frame))
    windows = _windows(frame)
    half = len(windows) // 2
    calibration = [window for window, _ts in windows[:half]]
    evaluation = windows[half:]
    if not calibration or not evaluation:
        raise RuntimeError(f"window split is empty: {len(windows)} windows")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    detector = AnomalyDetector(checkpoint_path=str(_CHECKPOINT))
    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    original_datetime = pipeline_module.datetime
    pipeline_module.datetime = _PinnedDatetime
    clean_flags: list[bool] = []
    clean_labels: list[str | None] = []
    injected: dict[str, list[tuple[bool, str | None]]] = {
        kind: [] for kind in _EXPECTED_LABEL
    }
    try:
        async with factory() as session:
            for index, (window, end_ts) in enumerate(evaluation):
                flagged, label = await _score_window(
                    session,
                    detector,
                    window,
                    end_ts,
                    f"accuracy-clean-{index}",
                    calibration,
                )
                clean_flags.append(flagged)
                clean_labels.append(label)
            for kind in _EXPECTED_LABEL:
                for seed in range(_N_TRIALS):
                    window = _inject(kind, _ideal_window(seed), seed)
                    flagged, label = await _score_window(
                        session,
                        detector,
                        window,
                        _NOON,
                        f"accuracy-{kind}-{seed}",
                        calibration,
                    )
                    injected[kind].append((flagged, label))
    finally:
        pipeline_module.datetime = original_datetime
        _PinnedDatetime.pinned = None
        logging.disable(previous_disable)
        await engine.dispose()

    n_clean = len(clean_flags)
    clean_flagged = sum(clean_flags)
    specificity = _rate(n_clean - clean_flagged, n_clean)
    recalls: dict[str, dict] = {}
    for kind, rows in injected.items():
        expected = _EXPECTED_LABEL[kind]
        flagged = sum(1 for is_anomaly, _label in rows if is_anomaly)
        correct = sum(1 for _flag, label in rows if label == expected)
        recalls[kind] = {
            "n": len(rows),
            "flagged": flagged,
            "detection_recall": _rate(flagged, len(rows)),
            "expected_label": expected,
            "label_correct": correct,
            "label_correct_rate": _rate(correct, len(rows)),
            "fault_types": _fault_counts([label for _flag, label in rows]),
        }
    mean_recall = float(
        np.mean([item["detection_recall"] for item in recalls.values()])
    )
    composite = 0.5 * specificity + 0.5 * mean_recall
    return {
        "co2_replaced": co2_span <= 1e-6,
        "co2_span_before_replace": co2_span,
        "n_windows": len(windows),
        "n_calibration_windows": len(calibration),
        "n_eval_windows": n_clean,
        "clean_flagged": clean_flagged,
        "clean_specificity": specificity,
        "clean_fpr": _rate(clean_flagged, n_clean),
        "clean_fault_types": _fault_counts(clean_labels),
        "recalls": recalls,
        "mean_detection_recall": mean_recall,
        "composite": composite,
        "composite_bar": _COMPOSITE_BAR,
    }


def _upload_spike_indices(n_rows: int) -> list[int]:
    """Twelve single-sample sites on the July week, away from the MAD warm-up."""
    candidates = np.arange(300, 1900, 40)
    if n_rows <= int(candidates[-1]):
        raise RuntimeError(f"July frame is shorter than the spike grid: {n_rows}")
    rng = np.random.default_rng(_UPLOAD_SPIKE_SEED)
    chosen = rng.choice(candidates, size=_UPLOAD_SPIKE_COUNT, replace=False)
    return sorted(int(index) for index in chosen)


def _hit_indices(frame: pd.DataFrame, *, column: str | None) -> tuple[list[int], Counter]:
    hits = _detect_upload_mad_spikes(frame, new_row_start=0)
    labels = [_timestamp_label(value) for value in frame["timestamp"].tolist()]
    index_of = {label: index for index, label in enumerate(labels)}
    by_column: Counter = Counter()
    matched: list[int] = []
    for hit in hits:
        by_column[str(hit.column)] += 1
        if column is not None and hit.column != column:
            continue
        index = index_of.get(hit.timestamp)
        if index is not None:
            matched.append(index)
    return matched, by_column


def _measure_upload() -> dict:
    frame = _load_july()
    events = _upload_spike_indices(len(frame))
    clean_idx, clean_columns = _hit_indices(frame, column="temperature")
    spiked = frame.copy()
    temperature = spiked["temperature"].to_numpy(dtype=float)
    temperature = temperature.copy()
    for index in events:
        temperature[index] += _SPIKE_DELTA_C
    spiked["temperature"] = temperature
    spike_idx, spike_columns = _hit_indices(spiked, column="temperature")
    spike_set = set(spike_idx)
    caught = sum(1 for index in events if index in spike_set)
    return {
        "method": "_detect_upload_mad_spikes",
        "not_used": "run_cold_run_upload",
        "n_rows": len(frame),
        "clean_temperature_hits": len(clean_idx),
        "clean_hits_by_column": dict(clean_columns),
        "spike_events": len(events),
        "spike_caught_at_index": caught,
        "spike_recall": _rate(caught, len(events)),
        "spike_hit_count": len(spike_idx),
        "spiked_hits_by_column": dict(spike_columns),
        "recall_bar": _UPLOAD_RECALL_BAR,
    }


@pytest.fixture(scope="module")
def pipeline_gate() -> dict:
    report = asyncio.run(_measure_pipeline())
    _record("pipeline", report)
    return report


def test_composite_detection_accuracy(pipeline_gate: dict) -> None:
    """Held-out specificity and injected recall, scored by the real pipeline."""
    composite = float(pipeline_gate["composite"])
    message = json.dumps(pipeline_gate, indent=2)
    assert pipeline_gate["co2_replaced"] is True, message
    assert composite >= _COMPOSITE_BAR, message


def test_upload_mad_july_spikes() -> None:
    """Clean July week has no MAD hits; each +6 °C sample is flagged."""
    report = _measure_upload()
    _record("upload", report)
    message = json.dumps(report, indent=2)
    assert report["clean_temperature_hits"] == 0, message
    assert sum(report["clean_hits_by_column"].values()) == 0, message
    assert report["spike_recall"] >= _UPLOAD_RECALL_BAR, message

"""Phase 1 accuracy gate. Bar is 90% composite, not a number to tune toward.

Composite = 0.5 * clean_specificity + 0.5 * mean detection recall on three
injected faults: a +6 °C spike on the timestep the enrichment spike vote reads, temperature noise
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

The empty-database run also records a temperature-spike amplitude sweep
and recall on a few real July eval windows. Only +6 °C on that sweep is
asserted, and only against the upload recall bar already used below.
+2 °C is recorded and is not a gate.

``ACCURACY_SEEDED=1`` (marker ``accuracy_seeded``) repeats the same
composite after inserting the July calibration rows as ``SensorReading``.
That path is skipped unless the env var is set. It asserts composite >=
``_COMPOSITE_BAR``, the same bar as the empty-database gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services import comprehensive_pipeline as pipeline_module
from src.api.services import diurnal_profile_service
from src.api.services.comprehensive_pipeline import (
    ComprehensiveInferencePipeline,
    _query_physical_history,
)
from src.api.services.diurnal_profile_service import build_diurnal_profiles
from src.api.services.upload_portions import (
    _detect_upload_mad_spikes,
    _timestamp_label,
)
from src.config import (
    DIURNAL_VOTE_MIN_DAYS,
    ENRICHMENT_SPIKE_CONFIRM_LAG,
    SENSOR_NAMES,
    SEQ_LEN,
)
from src.db.models import Base, SensorReading
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
_SPIKE_SWEEP_C = (2.0, 3.0, 4.0, 6.0)
_REAL_WINDOW_TRIALS = 12
_SEEDED_TENANT = "accuracy-july-seed"
_SEEDED_GREENHOUSE = "default"

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


def _seeded_requested() -> bool:
    return os.environ.get("ACCURACY_SEEDED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _spike_offset() -> int:
    """Index the enrichment spike vote reads. Negative; last sample when lag is 0."""
    return -1 - max(0, int(ENRICHMENT_SPIKE_CONFIRM_LAG))


def _apply_spike(window: np.ndarray, delta_c: float) -> np.ndarray:
    spiked = window.copy()
    spiked[_spike_offset(), 0] += delta_c
    return spiked


def _spread_indices(n_items: int, count: int) -> list[int]:
    if n_items <= 0 or count <= 0:
        return []
    if count >= n_items:
        return list(range(n_items))
    if count == 1:
        return [n_items // 2]
    return [round(i * (n_items - 1) / (count - 1)) for i in range(count)]


def _detection_block(rows: list[tuple[bool, str | None]]) -> dict:
    flagged = sum(1 for is_anomaly, _label in rows if is_anomaly)
    return {
        "n": len(rows),
        "flagged": flagged,
        "detection_recall": _rate(flagged, len(rows)),
        "fault_types": _fault_counts([label for _flag, label in rows]),
    }


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
        # The enrichment spike vote is cast for sample t-d, confirmed by the
        # d samples after it (d = ENRICHMENT_SPIKE_CONFIRM_LAG; 0 = the last
        # sample, unconfirmed). Inject on the sample that vote reads: a spike
        # on the last sample is not voted until the next reading arrives.
        return _apply_spike(window, _SPIKE_DELTA_C)
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
    pipeline: ComprehensiveInferencePipeline | None = None,
) -> tuple[bool, str | None]:
    _PinnedDatetime.pinned = end_ts
    normalized = normalize_data(window, SENSOR_RANGES)
    scorer = pipeline or ComprehensiveInferencePipeline(detector=detector)
    result = await scorer.run(
        customer_id=tenant,
        sequence=normalized.tolist(),
        db=session,
        greenhouse_id="default",
        sequence_timestamp=end_ts.isoformat(),
        noise_clean_windows=clean_windows,
    )
    fault = result.get("fault_type")
    return bool(result["is_anomaly"]), None if fault is None else str(fault)


async def _seed_calibration_rows(
    session: AsyncSession,
    frame: pd.DataFrame,
    n_rows: int,
) -> int:
    """Insert July rows before the first eval window. NULL source, one tenant."""
    values = frame.loc[:, SENSOR_NAMES].to_numpy(dtype=float)
    timestamps = frame["timestamp"].tolist()
    readings: list[SensorReading] = []
    for index in range(n_rows):
        row = values[index]
        if not np.isfinite(row).all():
            continue
        readings.append(
            SensorReading(
                tenant_id=_SEEDED_TENANT,
                greenhouse_id=_SEEDED_GREENHOUSE,
                timestamp=_as_utc(timestamps[index]),
                temperature=float(row[0]),
                humidity=float(row[1]),
                co2=float(row[2]),
                light=float(row[3]),
                source=None,
            )
        )
    session.add_all(readings)
    await session.commit()
    return len(readings)


async def _measure_pipeline(seeded: bool = False) -> dict:
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
    original_diurnal_datetime = diurnal_profile_service.datetime
    pipeline_module.datetime = _PinnedDatetime
    if seeded:
        # Diurnal profiles query wall-clock now, not the sample time. Pin the
        # same stand-in the physics rules already use, or July rows fall
        # outside the trailing history window.
        diurnal_profile_service.datetime = _PinnedDatetime
    shared = ComprehensiveInferencePipeline(detector=detector) if seeded else None
    clean_flags: list[bool] = []
    clean_labels: list[str | None] = []
    injected: dict[str, list[tuple[bool, str | None]]] = {
        kind: [] for kind in _EXPECTED_LABEL
    }
    seed_meta = {
        "n_seeded_rows": 0,
        "history_rows_before_first_eval": 0,
        "diurnal_sensors": [],
        "seed_cut": "rows before the first eval window",
        "pipeline_reused": seeded,
    }
    sweep_rows: dict[str, list[tuple[bool, str | None]]] = {}
    real_rows: dict[str, list[tuple[bool, str | None]]] = {}
    real_indices: list[int] = []
    started = time.perf_counter()
    try:
        async with factory() as session:
            if seeded:
                cut = half * _STRIDE
                if cut > len(frame):
                    raise RuntimeError(
                        f"calibration cut {cut} is past the July frame ({len(frame)})"
                    )
                seed_meta["n_seeded_rows"] = await _seed_calibration_rows(
                    session, frame, cut
                )
                first_end = evaluation[0][1]
                _PinnedDatetime.pinned = first_end
                history = await _query_physical_history(
                    session,
                    customer_id=_SEEDED_TENANT,
                    greenhouse_id=_SEEDED_GREENHOUSE,
                    before=first_end,
                )
                seed_meta["history_rows_before_first_eval"] = int(history.shape[0])
                profiles = await build_diurnal_profiles(
                    session,
                    tenant_id=_SEEDED_TENANT,
                    greenhouse_id=_SEEDED_GREENHOUSE,
                )
                seed_meta["diurnal_sensors"] = sorted(profiles)
            for index, (window, end_ts) in enumerate(evaluation):
                tenant = _SEEDED_TENANT if seeded else f"accuracy-clean-{index}"
                flagged, label = await _score_window(
                    session,
                    detector,
                    window,
                    end_ts,
                    tenant,
                    calibration,
                    shared,
                )
                clean_flags.append(flagged)
                clean_labels.append(label)
            for kind in _EXPECTED_LABEL:
                for seed in range(_N_TRIALS):
                    window = _inject(kind, _ideal_window(seed), seed)
                    tenant = _SEEDED_TENANT if seeded else f"accuracy-{kind}-{seed}"
                    flagged, label = await _score_window(
                        session,
                        detector,
                        window,
                        _NOON,
                        tenant,
                        calibration,
                        shared,
                    )
                    injected[kind].append((flagged, label))
            if not seeded:
                for delta in _SPIKE_SWEEP_C:
                    if delta == _SPIKE_DELTA_C:
                        continue
                    key = str(int(delta))
                    sweep_rows[key] = []
                    for seed in range(_N_TRIALS):
                        window = _apply_spike(_ideal_window(seed), delta)
                        flagged, label = await _score_window(
                            session,
                            detector,
                            window,
                            _NOON,
                            f"accuracy-sweep-{key}-{seed}",
                            calibration,
                        )
                        sweep_rows[key].append((flagged, label))
                real_indices = _spread_indices(len(evaluation), _REAL_WINDOW_TRIALS)
                for kind in ("spike", "level_shift"):
                    real_rows[kind] = []
                for offset, index in enumerate(real_indices):
                    base, end_ts = evaluation[index]
                    for kind in ("spike", "level_shift"):
                        flagged, label = await _score_window(
                            session,
                            detector,
                            _inject(kind, base, 10_000 + offset),
                            end_ts,
                            f"accuracy-real-{kind}-{offset}",
                            calibration,
                        )
                        real_rows[kind].append((flagged, label))
    finally:
        pipeline_module.datetime = original_datetime
        diurnal_profile_service.datetime = original_diurnal_datetime
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
    report = {
        "seeded": seeded,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
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
        "composite_bar_applied": True,
    }
    if seeded:
        report["seed"] = seed_meta
        return report
    sweep = {
        "confirm_lag": max(0, int(ENRICHMENT_SPIKE_CONFIRM_LAG)),
        "injection_offset": _spike_offset(),
        "recall_bar": _UPLOAD_RECALL_BAR,
        "recall_bar_source": "_UPLOAD_RECALL_BAR",
        "hard_bar_celsius": _SPIKE_DELTA_C,
        "plus_2_is_not_a_gate": True,
        "recalls": {},
    }
    for delta in _SPIKE_SWEEP_C:
        key = str(int(delta))
        if delta == _SPIKE_DELTA_C:
            spike = recalls["spike"]
            sweep["recalls"][key] = {
                "n": spike["n"],
                "flagged": spike["flagged"],
                "detection_recall": spike["detection_recall"],
                "same_trials_as": "recalls.spike",
            }
            continue
        sweep["recalls"][key] = _detection_block(sweep_rows[key])
    report["spike_amplitude_sweep"] = sweep
    report["real_window_recall"] = {
        "n": len(real_indices),
        "indices": real_indices,
        "spike_delta_c": _SPIKE_DELTA_C,
        "level_shift_temp_c": _LEVEL_SHIFT_TEMP_C,
        "level_shift_humidity": _LEVEL_SHIFT_HUMIDITY,
        "asserted": False,
        "spike": _detection_block(real_rows["spike"]),
        "level_shift": _detection_block(real_rows["level_shift"]),
    }
    return report


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


def test_spike_amplitude_sweep_keeps_plus_6(pipeline_gate: dict) -> None:
    """+6 °C stays at the upload recall bar. Smaller steps are recorded only."""
    sweep = pipeline_gate["spike_amplitude_sweep"]
    message = json.dumps(sweep, indent=2)
    recalls = sweep["recalls"]
    for delta in _SPIKE_SWEEP_C:
        key = str(int(delta))
        assert key in recalls, message
        recall = float(recalls[key]["detection_recall"])
        assert 0.0 <= recall <= 1.0, message
    assert float(recalls["6"]["detection_recall"]) >= _UPLOAD_RECALL_BAR, message
    assert sweep["plus_2_is_not_a_gate"] is True, message


def test_real_window_recall_is_recorded(pipeline_gate: dict) -> None:
    """Detection rate on real July eval windows is stored, not gated."""
    block = pipeline_gate["real_window_recall"]
    message = json.dumps(block, indent=2)
    assert block["asserted"] is False, message
    assert block["n"] == _REAL_WINDOW_TRIALS, message
    for kind in ("spike", "level_shift"):
        recall = float(block[kind]["detection_recall"])
        assert 0.0 <= recall <= 1.0, message


@pytest.mark.accuracy_seeded
@pytest.mark.skipif(
    not _seeded_requested(),
    reason="seeded accuracy gate is opt-in; set ACCURACY_SEEDED=1",
)
def test_seeded_database_records_composite() -> None:
    """Same composite after July calibration rows are in the database.

    Clean-flag and composite are gated at the same ``_COMPOSITE_BAR``
    as the empty-database run.
    """
    report = asyncio.run(_measure_pipeline(seeded=True))
    _record("pipeline_seeded", report)
    message = json.dumps(report, indent=2)
    assert report["seeded"] is True, message
    assert report["composite_bar_applied"] is True, message
    seed = report["seed"]
    # 996 seed rows are below the 2016-row (7-day) VOTE threshold, so the
    # pipeline suppresses the diurnal vote.  The profile service may still
    # build profiles (its own MIN_ROWS is 288), but the pipeline will not
    # cast a diurnal vote until DIURNAL_VOTE_MIN_DAYS is met.  This is the
    # fix for the 58/167 false-alarm problem.
    # 996 rows span ~3.5 calendar days, below the 7-day vote threshold.
    # The pipeline's maturity gate counts distinct days, not rows.
    assert seed["n_seeded_rows"] > 0, message
    assert seed["history_rows_before_first_eval"] >= 2 * len(SENSOR_NAMES), message
    # Clean flags must be <= 3 (down from 59 before the fix).
    assert report["clean_flagged"] <= 3, (
        f"seeded clean flags {report['clean_flagged']} should be <= 3"
    )
    composite = float(report["composite"])
    assert composite >= _COMPOSITE_BAR, message


def test_upload_mad_july_spikes() -> None:
    """Clean July week has no MAD hits; each +6 °C sample is flagged."""
    report = _measure_upload()
    _record("upload", report)
    message = json.dumps(report, indent=2)
    assert report["clean_temperature_hits"] == 0, message
    assert sum(report["clean_hits_by_column"].values()) == 0, message
    assert report["spike_recall"] >= _UPLOAD_RECALL_BAR, message

"""ADR-014 step A1: golden corpus and three-path scoring harness.

Test helper only. It builds a small set of physical-unit windows from
committed fixtures, scores each window through every entry point as it
exists today, and reduces each result to one comparable record.

Entry points (no production hook; each path is called as production does):

* ``upload``        -- ``run_cold_run_upload`` on a CSV of the case frame
  (``score_portion`` / ``finalize_scores``). Labels are the read-time map:
  ``build_trigger_data_by_timestamp`` then ``_fault_type_from_detector``.
* ``live``          -- ``LiveScorer.score`` fed the 20 window rows one tick
  at a time, real ``EnrichmentRunner`` (``AsyncSessionLocal`` points at a
  scratch SQLite file). The tick verdict is ``score >= threshold``; the
  label is ``fault_type`` on the ``anomaly-detail`` event.
* ``inference``     -- ``POST /api/inference`` through the test client.
* ``comprehensive`` -- ``POST /api/inference/comprehensive`` through the
  test client, with ``sequence_timestamp`` set to the window end. The ADR
  "Inference API" column covers both routes; the clock rows (12, 16, 26)
  only show between this route and live.

Rule votes are not on the live ``anomaly-detail`` event, and ``fault_type``
is not on the ``/comprehensive`` response (see the report). Both are read
from the dict ``ComprehensiveInferencePipeline.run`` returns, through a
pass-through spy that calls the original method unchanged.

Wall clock is frozen at ``FROZEN_NOW`` (a later machine date, noon) in every
module on these paths that reads ``datetime.now``. Every sequence carries its
own fixed timestamp, so a path that reads the clock instead of the data
shows up as a deterministic divergence, not a flaky one.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd

from src.config import ENRICHMENT_SPIKE_CONFIRM_LAG, LIVE_ANOMALY_THRESHOLD, SENSOR_NAMES, SEQ_LEN
from src.models.data_utils import SENSOR_RANGES, normalize_data
from tests.models.test_detection_accuracy import _load_csv, _plausible_co2

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
JULY_CSV = FIXTURES / "native_july_clearsky.csv"
DEC_CSV = FIXTURES / "native_dec_stuck_fault.csv"
CHECKPOINT = FIXTURES / "epoch_0072_trained_baseline.pt"
SNAPSHOT = FIXTURES / "scoring_parity_snapshot.json"

PATHS: tuple[str, ...] = ("upload", "live", "inference", "comprehensive")

# A later machine date at noon. A 02:00 sequence read on this clock is "day".
FROZEN_NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)

# Rows per case frame (window included). 8 h at 5 min: enough for the
# upload MAD window (10) and the 6 h wall-clock stuck rule; rolling-z (288)
# never warms up on a frame this short, which is recorded, not hidden.
FRAME_ROWS = 96

_DAY_END = pd.Timestamp("2026-07-02T12:00:00Z")
_NIGHT_END = pd.Timestamp("2026-07-03T02:00:00Z")
_STUCK_END = pd.Timestamp("2026-12-18T20:00:00Z")

_SPIKE_DELTA_C = 6.0          # accuracy-gate spike
_NOISE_SIGMA_C = 1.0          # accuracy-gate noise
_NOISE_SEED = 1000
_LEVEL_SHIFT_START = 8        # accuracy-gate level shift
_LEVEL_SHIFT_TEMP_C = 3.0
_LEVEL_SHIFT_HUMIDITY = -10.0
_OOB_TEMPERATURE_C = 85.0     # above OOB_LIMITS["temperature"] (80 °C)
_NIGHT_LIGHT_W_M2 = 300.0     # above the physics nocturnal-light floor (50)
_HEAT_EXCURSION_C = 30.0      # whole window ~55 C: in OOB limits, LSTM saturates

# Pipeline gate fault types that are not rule votes but are detector evidence.
_GATE_FAULTS = {"out_of_bounds": "out_of_bounds", "polling_dropout": "polling_dropout"}
_SCORE_DECIMALS = 6


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldenCase:
    """One window: ``frame`` is physical units, the window is its last 20 rows."""

    case_id: str
    frame: pd.DataFrame
    note: str

    @property
    def window(self) -> pd.DataFrame:
        return self.frame.iloc[-SEQ_LEN:]

    @property
    def window_array(self) -> np.ndarray:
        return self.window.loc[:, SENSOR_NAMES].to_numpy(dtype=np.float64)

    @property
    def window_end(self) -> datetime:
        return self.window["timestamp"].iloc[-1].to_pydatetime()


def _climate_frame(path: Path) -> pd.DataFrame:
    """Adapter-normalised fixture with the imputed constant CO2 replaced.

    Neither native fixture has a CO2 column; the adapter fills 400 ppm. A
    constant channel trips the 20-sample setpoint flatline rule on every
    window, so the accuracy gate's plausible trace is used instead.
    """
    frame = _load_csv(path).loc[:, ["timestamp", *SENSOR_NAMES]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["co2"] = _plausible_co2(len(frame))
    return frame.reset_index(drop=True)


def _slice_ending(frame: pd.DataFrame, end: pd.Timestamp) -> pd.DataFrame:
    hits = frame.index[frame["timestamp"] == end]
    if len(hits) != 1:
        raise RuntimeError(f"fixture has no unique row at {end}")
    stop = int(hits[0]) + 1
    sliced = frame.iloc[stop - FRAME_ROWS : stop].reset_index(drop=True).copy()
    if len(sliced) != FRAME_ROWS:
        raise RuntimeError(f"fixture too short before {end}")
    return sliced


def _with_window(frame: pd.DataFrame, column: str, values: np.ndarray) -> pd.DataFrame:
    out = frame.copy()
    col = out[column].to_numpy(dtype=float).copy()
    col[-SEQ_LEN:] = values
    out[column] = col
    return out


def _spike_index() -> int:
    """Window index the live/API spike vote reads (t - lag)."""
    return SEQ_LEN - 1 - max(0, int(ENRICHMENT_SPIKE_CONFIRM_LAG))


def build_corpus() -> list[GoldenCase]:
    """Nine windows from committed fixtures only (never the tenant corpus)."""
    july = _climate_frame(JULY_CSV)
    dec = _climate_frame(DEC_CSV)
    day = _slice_ending(july, _DAY_END)
    night = _slice_ending(july, _NIGHT_END)

    temp = day["temperature"].to_numpy(dtype=float)[-SEQ_LEN:]
    hum = day["humidity"].to_numpy(dtype=float)[-SEQ_LEN:]

    spike = temp.copy()
    spike[_spike_index()] += _SPIKE_DELTA_C

    noisy = temp + np.random.default_rng(_NOISE_SEED).normal(0.0, _NOISE_SIGMA_C, SEQ_LEN)

    shift_t = temp.copy()
    shift_t[_LEVEL_SHIFT_START:] += _LEVEL_SHIFT_TEMP_C
    shift_h = hum.copy()
    shift_h[_LEVEL_SHIFT_START:] += _LEVEL_SHIFT_HUMIDITY

    oob = temp.copy()
    oob[_spike_index()] = _OOB_TEMPERATURE_C

    night_light = np.full(SEQ_LEN, _NIGHT_LIGHT_W_M2)

    heat = temp + _HEAT_EXCURSION_C

    stuck = _slice_ending(dec, _STUCK_END)

    return [
        GoldenCase("clean_day", day, "July 2026-07-02 12:00, untouched"),
        GoldenCase("clean_night", night, "July 2026-07-03 02:00, untouched"),
        GoldenCase(
            "spike", _with_window(day, "temperature", spike),
            f"+{_SPIKE_DELTA_C} C on window index {_spike_index()} (t-lag)",
        ),
        GoldenCase(
            "noise", _with_window(day, "temperature", noisy),
            f"temperature + N(0, {_NOISE_SIGMA_C}) over the window",
        ),
        GoldenCase(
            "level_shift",
            _with_window(_with_window(day, "temperature", shift_t), "humidity", shift_h),
            f"+{_LEVEL_SHIFT_TEMP_C} C / {_LEVEL_SHIFT_HUMIDITY} %RH from index {_LEVEL_SHIFT_START}",
        ),
        GoldenCase(
            "oob", _with_window(day, "temperature", oob),
            f"temperature {_OOB_TEMPERATURE_C} C on window index {_spike_index()}",
        ),
        GoldenCase(
            "stuck", stuck,
            "December humidity frozen at 100.0 from 13:00 (fixture A main fault)",
        ),
        GoldenCase(
            "night_light", _with_window(night, "light", night_light),
            f"02:00 window with light {_NIGHT_LIGHT_W_M2} W/m2 (nocturnal-light rule)",
        ),
        GoldenCase(
            "heat_excursion", _with_window(day, "temperature", heat),
            f"temperature +{_HEAT_EXCURSION_C} C over the window (only case /api/inference flags)",
        ),
    ]


# --------------------------------------------------------------------------
# Frozen wall clock
# --------------------------------------------------------------------------

_REAL_DATETIME = datetime


class _FrozenMeta(type):
    def __instancecheck__(cls, obj: object) -> bool:  # keep isinstance(x, datetime)
        return isinstance(obj, _REAL_DATETIME)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, _REAL_DATETIME)


class FrozenDatetime(_REAL_DATETIME, metaclass=_FrozenMeta):
    """``datetime`` whose ``now``/``utcnow`` return ``FROZEN_NOW``."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        if tz is None:
            return FROZEN_NOW.replace(tzinfo=None)
        return FROZEN_NOW.astimezone(tz)

    @classmethod
    def utcnow(cls):  # noqa: D102
        return FROZEN_NOW.replace(tzinfo=None)


# Every module on the three paths that calls ``datetime.now``.
_CLOCK_MODULES: tuple[str, ...] = (
    "src.api.services.comprehensive_pipeline",
    "src.api.services.diurnal_profile_service",
    "src.api.routes.inference",
    "src.models.band_breach_detector",
    "src.models.setpoint_scada_detector",
    "src.models.operational_config_store",
    "src.models.customer_config_store",
    "src.models.active_learner",
    "src.models.anomaly_embedding",
)


@contextlib.contextmanager
def frozen_clock() -> Iterator[None]:
    import importlib

    with contextlib.ExitStack() as stack:
        for name in _CLOCK_MODULES:
            module = importlib.import_module(name)
            stack.enter_context(patch.object(module, "datetime", FrozenDatetime))
        yield


# --------------------------------------------------------------------------
# Normalisation helpers
# --------------------------------------------------------------------------


def _score(value: Any) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, _SCORE_DECIMALS)


def _pipeline_detectors(result: dict[str, Any]) -> list[str]:
    """Rule votes (precedence order) plus gate evidence from a pipeline dict."""
    from src.api.services.comprehensive_pipeline import _RULE_VOTE_PROFILES

    names = [k for k in (result.get("detector_scores") or {}) if k in _RULE_VOTE_PROFILES]
    gate = _GATE_FAULTS.get(str(result.get("fault_type")))
    if gate and gate not in names:
        names.append(gate)
    return names


def _pipeline_record(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {"ran": False}
    return {
        "ran": True,
        "is_anomaly": bool(result["is_anomaly"]),
        "fault_type": None if result.get("fault_type") is None else str(result["fault_type"]),
        "detectors": _pipeline_detectors(result),
        "affected_sensors": list(result.get("affected_sensors") or []),
        "physics": result.get("physics_validation") or None,
        "lstm_score": _score(result.get("anomaly_score")),
    }


class _PipelineSpy:
    """Pass-through wrapper over ``ComprehensiveInferencePipeline.run``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def installed(self) -> Iterator["_PipelineSpy"]:
        from src.api.services.comprehensive_pipeline import ComprehensiveInferencePipeline

        original = ComprehensiveInferencePipeline.run
        spy = self

        async def _run(pipeline_self, *args, **kwargs):
            result = await original(pipeline_self, *args, **kwargs)
            spy.calls.append({"kwargs": kwargs, "result": dict(result)})
            return result

        with patch.object(ComprehensiveInferencePipeline, "run", _run):
            yield self

    def take(self) -> list[dict[str, Any]]:
        calls, self.calls = self.calls, []
        return calls


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def _write_case_csv(case: GoldenCase, directory: Path) -> Path:
    path = directory / f"{case.case_id}.csv"
    out = case.frame.copy()
    out["timestamp"] = out["timestamp"].map(lambda ts: ts.isoformat())
    out.to_csv(path, index=False)
    return path


async def _run_upload(case: GoldenCase, csv_path: Path, detector: Any) -> dict[str, Any]:
    from src.api.services.anomaly_ranking import anomaly_rank_key
    from src.api.services.cold_run_upload import (
        build_trigger_data_by_timestamp,
        run_cold_run_upload,
    )
    from src.api.services.csv_persist import normalize_timestamp_key
    from src.api.services.fault_classification import _fault_type_from_detector
    from src.vendors.csv_adapter import CSVAdapter

    adapter = CSVAdapter({"filepath": str(csv_path)})
    adapter.connect()
    response, _ = await run_cold_run_upload(
        tmp_path=str(csv_path),
        adapter=adapter,
        detector=detector,
        tenant_id=f"parity-upload-{case.case_id}",
        greenhouse_id="default",
    )
    window_keys = {normalize_timestamp_key(ts.to_pydatetime()) for ts in case.window["timestamp"]}
    end_key = normalize_timestamp_key(case.window_end)

    window_end = case.window_end

    def _in_window(hit: Any) -> bool:
        if normalize_timestamp_key(hit.timestamp) in window_keys:
            return True
        # A stuck_fault hit is stamped at the freeze onset (detected_ts is
        # when the 6 h gate fired). It covers the window when the freeze
        # began before the window ended and was detected by its end.
        if hit.detector == "stuck_fault" and hit.detected_ts:
            onset = pd.Timestamp(hit.timestamp)
            detected = pd.Timestamp(hit.detected_ts)
            onset = onset.tz_localize("UTC") if onset.tzinfo is None else onset
            detected = detected.tz_localize("UTC") if detected.tzinfo is None else detected
            return onset <= window_end and detected <= window_end and detected >= onset
        return False

    hits = [h for h in response.high_anomalies if _in_window(h)]
    detectors = sorted({h.detector or "lstm" for h in hits})
    affected = sorted({h.column for h in hits if h.column})
    label = None
    if hits:
        triggers = build_trigger_data_by_timestamp(hits)
        top = max(hits, key=anomaly_rank_key)
        trigger = triggers[normalize_timestamp_key(top.timestamp)]
        label = _fault_type_from_detector(str(trigger["detector"]))

    lstm_score = None
    for ts, score in zip(response.sequence_timestamps, response.anomaly_scores):
        if normalize_timestamp_key(ts) == end_key:
            lstm_score = _score(score)
    return {
        "is_anomaly": bool(hits),
        "fault_type": label,
        "detectors": detectors,
        "affected_sensors": affected,
        "physics": None,
        "lstm_score": lstm_score,
        "lstm_threshold": _score(response.threshold_used),
        "hit_count_whole_frame": len(response.high_anomalies),
    }


class _Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def publish(self, tenant_id: str, greenhouse_id: str, kind: str, event: Any) -> None:
        self.events.append((kind, event))


class _Resolver:
    state = "ready"

    def __init__(self, detector: Any) -> None:
        self._detector = detector

    async def resolve(self) -> Any:
        return self._detector


async def _run_live(case: GoldenCase, detector: Any, spy: _PipelineSpy) -> dict[str, Any]:
    from src.live.enrichment import EnrichmentRunner
    from src.live.events import LiveReadingEvent
    from src.live.scoring import LiveScorer

    tenant = f"parity-live-{case.case_id}"
    hub = _Hub()
    resolver = _Resolver(detector)
    enrichment = EnrichmentRunner(
        tenant_id=tenant, greenhouse_id="default", hub=hub, resolver=resolver,
    )
    scorer = LiveScorer(
        tenant_id=tenant, greenhouse_id="default", hub=hub,
        resolver=resolver, enrichment=enrichment,
    )
    spy.take()
    scored = None
    for i, (ts, row) in enumerate(zip(case.window["timestamp"], case.window_array)):
        event = LiveReadingEvent(
            seq=f"{case.case_id}-{i}",
            timestamp=ts.to_pydatetime(),
            tenant_id=tenant,
            greenhouse_id="default",
            channels={name: float(v) for name, v in zip(SENSOR_NAMES, row)},
            sources={name: "parity" for name in SENSOR_NAMES},
        )
        scored = await scorer.score(event)
    pending = list(enrichment.tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    await scorer.close()

    score = None if scored is None else scored.anomaly_score
    flagged = score is not None and score >= LIVE_ANOMALY_THRESHOLD
    details = [e for kind, e in hub.events if kind == "anomaly-detail"]
    anomalies = [e for kind, e in hub.events if kind == "anomaly"]
    calls = spy.take()
    pipeline = _pipeline_record(calls[-1]["result"] if calls else None)
    return {
        "is_anomaly": bool(flagged),
        "fault_type": details[-1].fault_type if details else None,
        "detectors": (["lstm"] if flagged else []) + (pipeline.get("detectors") or []),
        "affected_sensors": list(anomalies[-1].affected_channels) if anomalies else [],
        "physics": pipeline.get("physics"),
        "lstm_score": _score(score),
        "enrichment": pipeline,
        "enrichment_got_timestamp": bool(calls and calls[-1]["kwargs"].get("sequence_timestamp")),
    }


def _run_inference(client: Any, case: GoldenCase) -> dict[str, Any]:
    from src.config import ANOMALY_THRESHOLD

    normalized = normalize_data(case.window_array.astype(np.float32), SENSOR_RANGES)
    resp = client.post(
        "/api/inference",
        json={
            "tenant_id": f"parity-inference-{case.case_id}",
            "greenhouse_id": "default",
            "sequences": [normalized.tolist()],
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"/api/inference {resp.status_code}: {resp.text}")
    body = resp.json()
    score = float(body["anomaly_scores"][0])
    flagged = score > ANOMALY_THRESHOLD
    high = body.get("high_anomalies") or []
    return {
        "is_anomaly": bool(flagged),
        "fault_type": None,
        "detectors": ["lstm"] if flagged else [],
        "affected_sensors": [],
        "physics": None,
        "lstm_score": _score(score),
        "high_anomaly_timestamp": high[0]["timestamp"] if high else None,
    }


def _run_comprehensive(client: Any, case: GoldenCase, spy: _PipelineSpy) -> dict[str, Any]:
    normalized = normalize_data(case.window_array.astype(np.float32), SENSOR_RANGES)
    spy.take()
    resp = client.post(
        "/api/inference/comprehensive",
        json={
            "customer_id": f"parity-comprehensive-{case.case_id}",
            "greenhouse_id": "default",
            "sequence": normalized.tolist(),
            "sequence_timestamp": case.window_end.isoformat(),
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"/api/inference/comprehensive {resp.status_code}: {resp.text}")
    body = resp.json()
    calls = spy.take()
    pipeline = _pipeline_record(calls[-1]["result"] if calls else None)
    return {
        "is_anomaly": bool(body["is_anomaly"]),
        "fault_type": pipeline.get("fault_type"),
        "detectors": pipeline.get("detectors") or [],
        "affected_sensors": list(body.get("affected_sensors") or []),
        "physics": pipeline.get("physics"),
        "lstm_score": _score(body.get("anomaly_score")),
        "response_has_fault_type": "fault_type" in body,
    }


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def run_harness(workdir: Path) -> dict[str, Any]:
    """Score every golden case through every path. Returns the snapshot dict."""
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from src.api.main import app
    from src.api.routes import inference_integrated
    from src.api.services.detector_cache import get_cached_detector
    from src.db.models import Base
    from src.db.session import get_db

    started = time.perf_counter()
    corpus = build_corpus()
    db_url = f"sqlite+aiosqlite:///{workdir / 'parity.db'}"

    async def _create_schema() -> None:
        engine = create_async_engine(db_url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_create_schema())

    stub_model = {
        "model_id": "parity_epoch_0072",
        "customer_id": "parity",
        "model_type": "lstm_vae",
        "checkpoint_path": str(CHECKPOINT),
        "metrics": {},
        "created_at": None,
    }
    detector = get_cached_detector(str(CHECKPOINT))
    spy = _PipelineSpy()
    results: dict[str, dict[str, Any]] = {c.case_id: {} for c in corpus}

    saved_pipeline_cache = dict(inference_integrated._pipeline_cache)
    inference_integrated._pipeline_cache.clear()
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(frozen_clock())
            stack.enter_context(spy.installed())
            stack.enter_context(
                patch(
                    "src.models.model_registry.ModelRegistry.get_active_model",
                    new=AsyncMock(return_value=stub_model),
                )
            )
            stack.enter_context(
                patch("src.live.runtime.LiveRuntime", side_effect=RuntimeError("disabled in parity harness"))
            )

            async def _offline_paths() -> None:
                engine = create_async_engine(db_url, poolclass=NullPool)
                factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
                try:
                    with patch("src.db.session.AsyncSessionLocal", factory):
                        for case in corpus:
                            csv_path = _write_case_csv(case, workdir)
                            results[case.case_id]["upload"] = await _run_upload(case, csv_path, detector)
                            results[case.case_id]["live"] = await _run_live(case, detector, spy)
                finally:
                    await engine.dispose()

            asyncio.run(_offline_paths())

            async def _override_get_db():
                engine = create_async_engine(db_url, poolclass=NullPool)
                factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
                try:
                    async with factory() as session:
                        yield session
                finally:
                    await engine.dispose()

            app.dependency_overrides[get_db] = _override_get_db
            try:
                with TestClient(app) as client:
                    for case in corpus:
                        results[case.case_id]["inference"] = _run_inference(client, case)
                        results[case.case_id]["comprehensive"] = _run_comprehensive(client, case, spy)
            finally:
                app.dependency_overrides.pop(get_db, None)
    finally:
        inference_integrated._pipeline_cache.clear()
        inference_integrated._pipeline_cache.update(saved_pipeline_cache)

    return {
        "frozen_now": FROZEN_NOW.isoformat(),
        "live_threshold": LIVE_ANOMALY_THRESHOLD,
        "cases": {
            c.case_id: {
                "note": c.note,
                "window_end": c.window_end.isoformat(),
                "paths": results[c.case_id],
            }
            for c in corpus
        },
        "_elapsed_seconds": round(time.perf_counter() - started, 2),
    }

"""ADR-014 step A3: the scoring core equals today's code, bit for bit.

Every core function is compared with the code it was copied from, called as
production calls it, on the parity corpus (``tests/parity/_golden_corpus.py``)
and on seeded random windows:

* ``ModelScorer`` vs ``AnomalyDetector.predict_batch_enriched`` (upload,
  API), ``ComprehensiveInferencePipeline._run_lstm_vae`` and the live
  ``forward_pass_sync`` (``src/live/attribution.py``, not edited).
* The window votes vs ``tests/fixtures/scoring_core_a3_pipeline_votes.json``.
  ADR-014 step A4 made ``_run_with_components`` call the core, so comparing
  the two live would be vacuous. The fixture is the output of the pre-A4
  pipeline (its private vote code, run unchanged up to ``_order_rule_votes``
  with a spy that recorded the ``rule_votes`` it received, the out-of-bounds
  gate caught at ``_persist_gate_result``, and the band findings caught at
  the detector) on exactly the cases ``_cases`` builds, frozen at A4. DB-backed
  inputs (seasonal scaler, diurnal profiles, operational config) were injected
  identically. Regenerating it from the current code proves nothing: change it
  only with a measured, intended behaviour change (Phase B).
* The batch look-ahead spike rows vs a golden frozen from the pre-A7
  ``_detect_upload_mad_spikes`` (``upload_spike_a3_golden.json``), and the
  upload scaled scores vs ``score_portion``.
"""

from __future__ import annotations

import ast
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
import pytest

from src.api.services import comprehensive_pipeline as cp
from src.api.services import upload_portions as up
from src import config
from src.config import (
    COLD_RUN_PERCENTILE,
    COLD_RUN_ROBUST_GATE_ENABLED,
    COLD_RUN_ROBUST_GATE_Z,
    LIVE_ANOMALY_THRESHOLD,
    MAX_OBSERVE_BATCH,
    SENSOR_NAMES,
    SEQ_LEN,
)
from src.live.attribution import forward_pass_sync
from src.models.cold_run_threshold import calibrate_threshold
from src.models.data_utils import SENSOR_RANGES, normalize_data, preprocess_sequences
from src.models import inference
from src.models.inference import AnomalyDetector, ModelScorer
from src.models.operational_config_store import OperationalConfigStore
from src.models.scoring import API_PROFILE, BATCH_PROFILE, STREAM_PROFILE, ScoringContext
from src.models.scoring.lstm import (
    lstm_flag,
    lstm_threshold,
    normalise_window,
    scaled_scores,
)
from src.models.scoring.votes import (
    change_point_vote,
    diurnal_vote,
    noise_vote,
    oob_vote,
    spike_mad_rows,
    spike_mad_vote,
    window_state,
)
from src.models.scoring.window_rules import band_findings, dual_sensor_votes, setpoint_votes
from src.models.seasonal_scaler import SeasonalScaler
from src.models.scoring.stream_stages import UploadStreamState, clamp_frame
from tests.parity._golden_corpus import CHECKPOINT, build_corpus

SEED = 20261124
_SPIKE_GOLDEN = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "upload_spike_a3_golden.json").read_text()
)
N_RANDOM = 64
TENANT = "a3-tenant"
GREENHOUSE = "a3-gh"
CORPUS = build_corpus()
GOLDEN_VOTES = Path(__file__).resolve().parents[1] / "fixtures" / "scoring_core_a3_pipeline_votes.json"

# Pipeline order of the votes before _order_rule_votes (steps 0a+ .. 0h).
_CORE_VOTES = (setpoint_votes, noise_vote, spike_mad_vote, dual_sensor_votes,
               diurnal_vote, change_point_vote)


@pytest.fixture(scope="module")
def detector() -> AnomalyDetector:
    return AnomalyDetector(str(CHECKPOINT))


class _History:
    """``HistoryProvider`` over fixed values (what the A5 adapter will prefetch)."""

    def __init__(self, config: Mapping[str, Any], scaler: SeasonalScaler,
                 profiles: Mapping[str, Any], clean: Sequence[Any]) -> None:
        self._config, self._scaler, self._profiles, self._clean = config, scaler, profiles, clean

    def diurnal_profile(self, anchor_date: Any) -> Optional[Mapping[str, Any]]:
        return self._profiles

    def noise_clean_windows(self) -> Sequence[Any]:
        return self._clean

    def operational_overrides(self) -> Mapping[str, Any]:
        return self._config

    def seasonal_scaler(self) -> Optional[Any]:
        return self._scaler


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def _physical_window(rng: np.random.Generator) -> np.ndarray:
    """A plausible window with one random fault shape (or none)."""
    t = np.arange(SEQ_LEN)
    w = np.column_stack([
        rng.uniform(15, 32) + rng.normal(0, 0.15, SEQ_LEN) + rng.uniform(-0.05, 0.05) * t,
        rng.uniform(50, 92) + rng.normal(0, 0.6, SEQ_LEN),
        rng.uniform(350, 900) + rng.normal(0, 8, SEQ_LEN),
        np.full(SEQ_LEN, 0.0) if rng.random() < 0.3 else rng.uniform(50, 800) + rng.normal(0, 10, SEQ_LEN),
    ])
    kind = rng.integers(0, 8)
    col = int(rng.integers(0, 2))
    if kind in (1, 6):  # spike, mostly at the voted index t-1, near the floor
        idx = SEQ_LEN - 2 if rng.random() < 0.7 else int(rng.integers(SEQ_LEN - 4, SEQ_LEN))
        w[idx, col] += rng.choice([-1, 1]) * rng.uniform(0.5, 9)
    elif kind == 2:  # level shift on temperature + humidity
        start = int(rng.integers(4, 16))
        w[start:, 0] += rng.uniform(1, 5)
        w[start:, 1] -= rng.uniform(3, 15)
    elif kind == 3:  # noise burst
        w[:, col] += rng.normal(0, rng.uniform(0.3, 2.0), SEQ_LEN)
    elif kind == 4:  # flatline on a random channel
        w[:, int(rng.integers(0, 4))] = w[0, int(rng.integers(0, 4))]
    elif kind == 5:  # out of bounds
        w[int(rng.integers(0, SEQ_LEN)), 0] = rng.choice([85.0, -45.0])
    elif kind == 7:  # one-channel shift at nightfall: light must not be the 2nd channel
        start = int(rng.integers(4, 16))
        w[start:, col] += rng.uniform(2, 10)
        w[:start, 3], w[start:, 3] = rng.uniform(300, 700), 0.0
    return w


def _random_case(rng: np.random.Generator, profile_name: str) -> dict[str, Any]:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * int(rng.integers(0, 105_000)))
    store = OperationalConfigStore()
    overrides: dict[str, Any] = {}
    if rng.random() < 0.5:
        overrides["crop"] = str(rng.choice(["tomato", "cucumber", "strawberry"]))
    if rng.random() < 0.3:
        overrides["season_map"] = {str(m): str(rng.choice(["summer", "winter", "spring"])) for m in range(1, 13)}
    if overrides:
        store.set_overrides(TENANT, GREENHOUSE, overrides)
    scaler = SeasonalScaler(overrides={
        "summer": float(rng.uniform(0.5, 2.0)) if rng.random() < 0.6 else None,
        "winter": float(rng.uniform(0.5, 2.0)) if rng.random() < 0.6 else None,
    })
    window = _physical_window(rng)
    profiles: dict[str, Any] = {}
    if rng.random() < 0.5:
        profiles = {s: {h: (float(window[-1, i] + rng.normal(0, 3)), float(rng.uniform(0.05, 2)))
                        for h in range(24)} for i, s in enumerate(SENSOR_NAMES)}
    clean: list[np.ndarray] = []
    pairs = None
    climate = {"t_setpoint": float(rng.uniform(15, 28)), "t_plant": float(rng.uniform(15, 30)),
               "t_outdoor": float(rng.uniform(-5, 30)), "i_solar": float(rng.uniform(0, 800)),
               "measured_hd": float(rng.uniform(0, 12))} if rng.random() < 0.6 else None
    if profile_name == "stream":
        clean = [_physical_window(np.random.default_rng(int(rng.integers(1 << 30))))
                 for _ in range(int(rng.integers(0, 6)))]
    elif rng.random() < 0.6:
        a = window[:, 0]
        b = a + rng.uniform(-3, 3) if rng.random() < 0.5 else rng.normal(a.mean(), 2, SEQ_LEN)
        cut = int(rng.choice([SEQ_LEN, SEQ_LEN, SEQ_LEN - 3, 1]))  # mismatched / too short skip
        pairs = [{"variable": "temperature", "sensor_a": a.tolist(), "sensor_b": list(b)[:cut]}]
    return {"window": window, "ts": ts, "store": store, "scaler": scaler, "profiles": profiles,
            "clean": clean, "climate": climate, "pairs": pairs}


def _corpus_case(case: Any, profile_name: str) -> dict[str, Any]:
    return {"window": case.window_array, "ts": case.window_end, "store": OperationalConfigStore(),
            "scaler": SeasonalScaler(), "profiles": {}, "clean": [], "climate": None, "pairs": None,
            "id": case.case_id}


def _cases(profile_name: str) -> list[dict[str, Any]]:
    rng = np.random.default_rng(SEED + (profile_name == "api"))
    return [_corpus_case(c, profile_name) for c in CORPUS] + [
        _random_case(rng, profile_name) for _ in range(N_RANDOM)
    ]


# --------------------------------------------------------------------------
# Old side: the pre-A4 pipeline's votes, frozen
# --------------------------------------------------------------------------


def _golden(variant: str) -> list[dict[str, Any]]:
    """The frozen pre-A4 pipeline output for ``variant``, one dict per case.

    Each dict has ``band`` (band findings), and either ``gate`` (out-of-bounds
    sensors, the window was gated) or ``votes`` (``[key, sensors]`` pairs in
    the pipeline's order before ``_order_rule_votes``).
    """
    return json.loads(GOLDEN_VOTES.read_text())[variant]


def _context(case: dict[str, Any], profile: Any) -> ScoringContext:
    return ScoringContext(
        tenant_id=TENANT, greenhouse_id=GREENHOUSE,
        sequence_timestamp=datetime.fromisoformat(case["ts"].isoformat()), profile=profile,
        history=_History(case["store"].get(TENANT, GREENHOUSE), case["scaler"], case["profiles"], case["clean"]),
        climate=case["climate"], sensor_pairs=tuple(case["pairs"] or ()),
    )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


_FACTOR_MODULES = ("src.models.inference", "src.api.services.upload_portions")


def _scaling_factor(monkeypatch: pytest.MonkeyPatch, factor: Optional[float]) -> None:
    """Patch ANOMALY_SCALING_FACTOR on both sides. The shipped 2.0 is a power of
    two, so float32 and float64 division agree; 3.0 shows each path's arithmetic."""
    if factor is not None:
        for module in _FACTOR_MODULES:
            monkeypatch.setattr(f"{module}.ANOMALY_SCALING_FACTOR", factor)


@pytest.mark.parametrize("factor", [None, 3.0], ids=["shipped", "factor3"])
def test_model_scorer_equals_every_forward_pass_copy(
    detector: AnomalyDetector, factor: Optional[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    _scaling_factor(monkeypatch, factor)
    rng = np.random.default_rng(SEED)
    corpus = [normalize_data(c.window_array.astype(np.float32), SENSOR_RANGES) for c in CORPUS]
    random = [normalize_data(_physical_window(rng).astype(np.float32), SENSOR_RANGES)
              for _ in range(MAX_OBSERVE_BATCH + 9)]  # crosses one chunk boundary
    scorer = ModelScorer.from_detector(detector)

    for batch in (corpus, corpus + random):
        got = scorer.predict_with_channel_errors(batch)
        raw, emb, unc = detector.predict_batch_enriched(batch)
        assert got.raw.dtype == raw.dtype and np.array_equal(got.raw, raw)
        assert np.array_equal(got.embeddings, emb) and np.array_equal(got.uncertainties, unc)

    pipeline = cp.ComprehensiveInferencePipeline(detector=detector)
    for window in corpus + random[:24]:
        one = scorer.predict_with_channel_errors([window])
        live_scaled, live_channels = forward_pass_sync(detector, window)
        assert scaled_scores(one, STREAM_PROFILE) == [live_scaled]
        assert np.array_equal(one.per_channel[0], live_channels)
        raw_score, api_scaled, embedding, uncertainty = pipeline._run_lstm_vae(window)
        assert float(one.raw[0]) == raw_score
        assert scaled_scores(one, API_PROFILE) == [api_scaled]
        # A4: the pipeline's scale is the core's; pin it to the plain formula too.
        assert api_scaled == float(np.clip(raw_score / inference.ANOMALY_SCALING_FACTOR, 0.0, 1.0))
        assert one.embeddings[0].tolist() == embedding and float(one.uncertainties[0]) == uncertainty

    empty = scorer.predict_with_channel_errors(np.zeros((0, SEQ_LEN, 4), dtype=np.float32))
    assert empty.raw.size == 0 and scaled_scores(empty, STREAM_PROFILE) == []


def test_normalisation_follows_each_path(detector: AnomalyDetector) -> None:
    window = CORPUS[0].window_array
    assert np.array_equal(normalise_window(window, STREAM_PROFILE),
                          normalize_data(np.array(window, dtype=np.float32), SENSOR_RANGES))
    normalised = normalise_window(window, STREAM_PROFILE)
    assert np.array_equal(normalise_window(normalised.tolist(), API_PROFILE), normalised)
    with pytest.raises(ValueError):
        normalise_window(window, BATCH_PROFILE)


@pytest.mark.parametrize("factor", [None, 3.0], ids=["shipped", "factor3"])
def test_upload_scores_and_spike_rows_equal_score_portion(
    detector: AnomalyDetector, factor: Optional[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    _scaling_factor(monkeypatch, factor)
    rng = np.random.default_rng(SEED)
    scorer = ModelScorer.from_detector(detector)
    frames = [c.frame for c in CORPUS]
    for _ in range(12):
        frame = CORPUS[int(rng.integers(len(CORPUS)))].frame.copy()
        temp = frame["temperature"].to_numpy(dtype=float)
        for idx in [SEQ_LEN - 1, *rng.integers(10, len(frame), size=4)]:
            temp[idx] += rng.choice([-1, 1]) * rng.uniform(0.5, 6)
            if rng.random() < 0.5 and idx + 1 < len(frame):
                temp[idx + 1] += rng.uniform(-3, 3)
        frame["temperature"] = temp
        frames.append(frame)
    ctx = ScoringContext(tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                         sequence_timestamp=CORPUS[0].window_end, profile=BATCH_PROFILE)
    for n, frame in enumerate(frames):
        ranges = [(float(frame[s].min()) - 1.0, float(frame[s].max()) + 1.0) for s in SENSOR_NAMES]
        result, _, _ = up.score_portion(
            normalized_df=frame.copy(), prev_overlap_df=pd.DataFrame(), raw_rows=[],
            state=UploadStreamState.initial(SEQ_LEN), adaptive_ranges=ranges, detector=detector,
        )
        # The model input is the clamped frame, as score_portion builds it.
        clamped = clamp_frame(frame)
        model_in = preprocess_sequences(normalise_window(clamped[SENSOR_NAMES].to_numpy(), BATCH_PROFILE, ranges))
        assert scaled_scores(scorer.predict_with_channel_errors(model_in), BATCH_PROFILE) == result.scores
        labels = frame["timestamp"].tolist()
        columns = {s: frame[s].to_numpy(dtype=float) for s in SENSOR_NAMES}
        if n % 2:  # an Inf reading is flagged and confirmed, never reported
            frame = frame.copy()
            frame.loc[SEQ_LEN + 3, "temperature"] = np.inf
            columns = {s: frame[s].to_numpy(dtype=float) for s in SENSOR_NAMES}
        for start in (0, 5, SEQ_LEN - 1):
            # Frozen from the pre-A7 ``_detect_upload_mad_spikes`` (generated on
            # the unmodified code, same frames, tests/fixtures/upload_spike_a3_golden.json).
            old = [tuple(h) for h in _SPIKE_GOLDEN[f"{n}:{start}"]]
            new = [(r.column, up._timestamp_label(labels[r.index]), r.value)
                   for r in spike_mad_rows(columns, ctx, BATCH_PROFILE, start)]
            assert new == old


def test_lstm_threshold_and_flag_reproduce_each_path() -> None:
    scores = np.random.default_rng(SEED).gamma(2.0, 0.01, 5000).tolist()
    file_t = lstm_threshold(BATCH_PROFILE, file_scores=scores)
    assert (file_t.value, file_t.method) == up._calibrate_upload_threshold(np.asarray(scores))
    explicit = calibrate_threshold(
        np.asarray(scores), percentile=COLD_RUN_PERCENTILE,
        robust_gate_z=COLD_RUN_ROBUST_GATE_Z if COLD_RUN_ROBUST_GATE_ENABLED else None)
    assert explicit == calibrate_threshold(np.asarray(scores))
    assert lstm_flag(file_t.value, file_t.value, BATCH_PROFILE) is False  # upload uses '>'

    live_t = lstm_threshold(STREAM_PROFILE).value
    assert live_t == LIVE_ANOMALY_THRESHOLD
    assert lstm_flag(live_t, live_t, STREAM_PROFILE) is True  # live uses '>=' (OQ4 kept)

    fallback = float(config.ANOMALY_THRESHOLD)
    assert lstm_threshold(API_PROFILE).value == fallback
    assert lstm_threshold(API_PROFILE, tracker_thresholds=(0.3, 0.4)).value == 0.4
    assert lstm_threshold(API_PROFILE, tracker_thresholds=(math.inf, 0.1)).value == fallback
    assert lstm_flag(0.4, 0.4, API_PROFILE) is False
    with pytest.raises(ValueError):
        lstm_threshold(BATCH_PROFILE)


@pytest.mark.parametrize(
    ("profile", "no_noise_baseline"),
    [(STREAM_PROFILE, False), (API_PROFILE, False), (API_PROFILE, True)],
    ids=["stream", "api", "api-fixed-noise-target"],
)
def test_window_votes_equal_pipeline(
    profile: Any, no_noise_baseline: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    if no_noise_baseline:
        # Every shipped sensor has a baseline variance, so the seasonal
        # noise-target branch is unreachable at defaults; remove the map to
        # compare that branch too (both sides build NoiseDetector per call).
        monkeypatch.setattr("src.models.noise_detector.NOISE_BASELINE_VARIANCE", {})
    fired: set[str] = set()
    variant = "api-fixed-noise-target" if no_noise_baseline else profile.name
    cases = _cases(profile.name)
    golden = _golden(variant)
    assert len(golden) == len(cases)
    for case, old in zip(cases, golden):
        seq_array = normalize_data(case["window"].astype(np.float32), SENSOR_RANGES)
        window = cp._denormalize_sequence(seq_array)
        ctx = _context(case, profile)
        state = window_state(ctx)
        oob = oob_vote(window, ctx, profile, state)
        assert list(oob.votes.get("out_of_bounds", ())) == old.get("gate", [])
        if "gate" in old:
            fired.add("out_of_bounds")
            continue
        new = [[k, list(v)] for fn in _CORE_VOTES for k, v in fn(window, ctx, profile, state).votes.items()]
        assert new == old["votes"], case.get("id")
        assert list(band_findings(window, ctx, profile, state).findings) == old["band"]
        fired.update(k for k, _ in new)
        fired.update("band" for _ in old["band"][:1])
    # The corpus and the seeded draw must exercise the votes, or equality is vacuous.
    assert {"spike_mad", "noise", "change_point", "out_of_bounds", "sensor_flatline", "diurnal"} <= fired


def test_votes_honour_profile_gates() -> None:
    case = _corpus_case(CORPUS[2], "batch")
    window = case["window"]
    with pytest.raises(ValueError):
        spike_mad_vote(window, _context(case, BATCH_PROFILE), BATCH_PROFILE)
    for fn in (noise_vote, diurnal_vote, change_point_vote, setpoint_votes, band_findings):
        assert not fn(window, _context(case, BATCH_PROFILE), BATCH_PROFILE).fired
        assert fn(window, _context(case, BATCH_PROFILE), BATCH_PROFILE).findings == ()
    pairs = [{"variable": "temperature", "sensor_a": [1.0] * SEQ_LEN, "sensor_b": [9.0] * SEQ_LEN}]
    ctx = ScoringContext(tenant_id=TENANT, greenhouse_id=GREENHOUSE, sequence_timestamp=CORPUS[2].window_end,
                         profile=STREAM_PROFILE, sensor_pairs=tuple(pairs))
    assert not dual_sensor_votes(window, ctx, STREAM_PROFILE).fired
    # No history: code defaults, identity scaler, still the spike vote of the corpus case.
    bare = ScoringContext(tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                          sequence_timestamp=CORPUS[2].window_end, profile=STREAM_PROFILE)
    assert dict(spike_mad_vote(window, bare, STREAM_PROFILE).votes) == {"spike_mad": ("temperature",)}


def test_core_package_is_pure_and_reads_no_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    package = Path(__file__).resolve().parents[2] / "src" / "models" / "scoring"
    banned_calls = {("datetime", "now"), ("datetime", "utcnow"), ("time", "time"), ("time", "monotonic")}
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                assert not any(n.split(".")[:2] in (["src", "api"], ["src", "db"], ["src", "live"])
                               for n in names), path.name
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert (node.value.id, node.attr) not in banned_calls, f"{path.name}: {node.value.id}.{node.attr}"

    class _NoClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # pragma: no cover - must not run
            raise AssertionError("wall clock read inside the scoring core")

    for module in ("src.models.band_breach_detector", "src.models.setpoint_scada_detector"):
        monkeypatch.setattr(f"{module}.datetime", _NoClock)
    case = _corpus_case(CORPUS[1], "api")  # 02:00 night window
    ctx = _context(case, API_PROFILE)
    for fn in _CORE_VOTES + (band_findings, oob_vote):
        fn(case["window"], ctx, API_PROFILE)

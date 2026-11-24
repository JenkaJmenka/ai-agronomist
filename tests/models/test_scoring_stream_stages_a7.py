"""ADR-014 step A7 (ml half): upload stream stages equal ``score_portion``.

The parity corpus frames and the two committed native fixtures are read the
way ``run_cold_run_upload`` reads them (``_read_chunks``, adapter normalise,
``parse_raw_rows`` / ``_strip_raw_records``), split into portions of several
sizes, and scored twice: through today's ``score_portion`` and through
``score_upload_stream`` with its carried state. OOB, duration-stuck,
rolling-z and look-ahead spike outputs and every carried state must be equal
after every portion, and the end-of-file stuck flush too.

The LSTM is not under test here (A3 covers ``ModelScorer``); a zero-score
stub stands in so ``score_portion`` runs its full body.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd
import pytest

from src.api.schemas.sensor_data import StatisticalAnomalyDetail, UploadHighAnomalyDetail
from src.api.services import upload_portions as up
from src.api.services.cold_run_upload import _read_chunks, _strip_raw_records
from src.api.services.upload_producer import _normalized_records, _raw_records
from src.config import COLD_RUN_STAT_MIN_CONSECUTIVE, COLD_RUN_STAT_ROLLING_WINDOW, LATENT_DIM, SEQ_LEN
from src.models.scoring import BATCH_PROFILE, STREAM_PROFILE, ScoringContext
from src.models.scoring.stream_stages import (
    RollingZState,
    RowHit,
    SpikeTailState,
    StuckStageState,
    UploadStreamOutput,
    UploadStreamState,
    clamp_frame,
    duration_stuck_stage,
    flush_duration_stuck,
    oob_clamp_stage,
    rolling_z_stage,
    score_upload_stream,
    spike_lookahead_stage,
    timestamp_label,
)
from src.models.stuck_fault_detector import StuckCarry
from src.vendors.csv_adapter import CSVAdapter, parse_raw_rows
from tests.parity._golden_corpus import (
    DEC_CSV,
    JULY_CSV,
    GoldenCase,
    _climate_frame,
    _write_case_csv,
    build_corpus,
)

CORPUS_SIZES = (7, 33, 94, 95, 96)        # 95: the corpus spike row ends portion 1
FIXTURE_SIZES = (97, 701, 5000)
SPIKE_C = 6.0                             # the accuracy gate's spike amplitude
CTX = ScoringContext(tenant_id="a7", greenhouse_id="gh", sequence_timestamp=pd.Timestamp(
    "2026-07-02T12:00:00Z").to_pydatetime(), profile=BATCH_PROFILE)
_STAT_CARRY_LEN = COLD_RUN_STAT_ROLLING_WINDOW + COLD_RUN_STAT_MIN_CONSECUTIVE - 1


class _ZeroDetector:
    """``predict_batch_enriched`` stand-in: the LSTM is out of scope here."""

    def predict_batch_enriched(self, sequences: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n = len(sequences)
        return np.zeros(n), np.zeros((n, LATENT_DIM)), np.zeros(n)


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, Path, tuple[int, ...]]]:
    workdir = tmp_path_factory.mktemp("a7_corpus")
    corpus = [(c.case_id, _write_case_csv(c, workdir), CORPUS_SIZES) for c in build_corpus()]
    # July with +6 C one-row spikes on, just before and just after 97-row
    # portion boundaries, so confirmation often arrives in the next portion.
    july = _climate_frame(JULY_CSV)
    temp = july["temperature"].to_numpy(dtype=float).copy()
    for k in range(2, len(july) // 97, 2):
        temp[97 * k + (k % 3) - 2] += SPIKE_C
    july["temperature"] = temp
    spiked = _write_case_csv(GoldenCase("july_spiked", july, "A7 boundary spikes"), workdir)
    return corpus + [("july", JULY_CSV, FIXTURE_SIZES), ("dec", DEC_CSV, FIXTURE_SIZES),
                     ("july_spiked", spiked, FIXTURE_SIZES)]


def _portions(path: Path, size: int) -> Iterator[tuple[pd.DataFrame | None, pd.DataFrame]]:
    """(normalised chunk or None, parsed raw frame), as ``run_cold_run_upload`` reads them."""
    adapter = CSVAdapter({"filepath": str(path)})
    adapter.connect()
    for raw_chunk in _read_chunks(str(path), size, adapter._csv_props):
        if raw_chunk.empty:
            continue
        try:
            normalized: pd.DataFrame | None = adapter.normalize(raw_chunk)
            adapter.validate(normalized, allow_oob=True)
        except Exception:
            normalized = None
        raw_df, _ = parse_raw_rows(raw_chunk)
        yield normalized, raw_df


def _imputed(path: Path) -> list[str]:
    from src.api.services.cold_run_upload import detect_optional_sensor_presence

    adapter = CSVAdapter({"filepath": str(path)})
    adapter.connect()
    header = list(next(iter(_read_chunks(str(path), 5, adapter._csv_props))).columns)
    return sorted(detect_optional_sensor_presence(header)[1])


def _nan_safe(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, dict):
        return {k: _nan_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_nan_safe(v) for v in value]
    return value


def _row_detail(hit: RowHit) -> dict[str, Any]:
    return UploadHighAnomalyDetail(score=1.0, timestamp=hit.timestamp, detector=hit.detector,
                                   column=hit.column, severity=hit.severity, method=hit.method,
                                   value=hit.value).model_dump()


def _as_portion(out: UploadStreamOutput) -> dict[str, list[dict[str, Any]]]:
    """The backend mapping onto today's ``PortionScores`` fields."""
    return {
        "oob": [_row_detail(h) for h in out.oob_hits],
        "stuck": [up._stuck_dict_to_high_detail(a.as_dict()).model_dump() for a in out.stuck_alerts],
        "stat": [StatisticalAnomalyDetail(timestamp=h.timestamp, column=h.column, value=h.value,
                                          z_score=float(h.z_score), severity=h.severity,
                                          method=h.method).model_dump() for h in out.rolling_z_hits],
        "mad": [_row_detail(h) for h in out.spike_hits],
    }


def _of_portion(p: up.PortionScores) -> dict[str, list[dict[str, Any]]]:
    return {"oob": [h.model_dump() for h in p.oob_hits], "stuck": [h.model_dump() for h in p.stuck_hits],
            "stat": [h.model_dump() for h in p.stat_hits], "mad": [h.model_dump() for h in p.mad_hits]}


def _assert_state_equal(state: UploadStreamState, stat: up.StatCarry, stuck: StuckCarry,
                        overlap: pd.DataFrame) -> None:
    assert state.stuck is not None and state.stuck.to_carry() == stuck
    assert state.stuck == StuckStageState.from_carry(stuck)
    z = state.rolling_z
    assert _nan_safe([dict(r) for r in z.rows]) == _nan_safe(stat.rows)
    assert (z.total_rows, z.columns, set(z.seen)) == (
        stat.total_rows, None if stat.columns is None else tuple(stat.columns), stat.seen)
    assert dict(z.psychro_context) == stat.psychro_context
    tail = state.spike_tail
    if overlap.empty:
        assert tail.labels == ()
        return
    assert list(tail.labels) == [timestamp_label(t) for t in overlap["timestamp"].tolist()]
    for sensor, values in tail.values.items():
        assert _nan_safe(list(values)) == _nan_safe(overlap[sensor].astype(float).tolist())


def _run_both(path: Path, size: int, imputed: list[str], *, json_hop: bool) -> dict[str, int]:
    """Score ``path`` in ``size`` portions both ways; return coverage counts."""
    stat, stuck, overlap = up.StatCarry(), StuckCarry(skip_channels=frozenset(imputed)), pd.DataFrame()
    state = UploadStreamState.initial(SEQ_LEN, imputed)
    seen = {"portions": 0, "oob": 0, "stuck": 0, "stat": 0, "mad": 0,
            "mad_on_tail_row": 0, "boundary_in_fired_freeze": 0}
    for normalized, raw_df in _portions(path, size):
        raw_rows = _strip_raw_records(raw_df) if not raw_df.empty else []
        open_fired = state.stuck.to_carry() if state.stuck else None
        if seen["portions"] and open_fired and any(
                open_fired.has_onset_fired(ch, st.onset_ts) for ch, st in open_fired.channels.items()):
            seen["boundary_in_fired_freeze"] += 1
        before, before_dump = state, _nan_safe(state.to_dict())
        tail_labels = set(state.spike_tail.labels)
        out, state = score_upload_stream(
            None if normalized is None else normalized.copy(), raw_rows, state, CTX, BATCH_PROFILE)
        assert _nan_safe(before.to_dict()) == before_dump  # the input state is never mutated
        portion, overlap = up.score_portion(
            normalized_df=normalized, prev_overlap_df=overlap, raw_rows=raw_rows, stat_carry=stat,
            adaptive_ranges=[(0.0, 1.0)] * 4, detector=_ZeroDetector(), seq_len=SEQ_LEN, stuck_carry=stuck)
        got, want = _as_portion(out), _of_portion(portion)
        assert got == want, f"{path.name} size={size} portion={seen['portions']}"
        _assert_state_equal(state, stat, stuck, overlap)
        if json_hop:
            state = UploadStreamState.from_dict(json.loads(json.dumps(state.to_dict())))
        seen["portions"] += 1
        for key in ("oob", "stuck", "stat", "mad"):
            seen[key] += len(want[key])
        new_labels = {h.timestamp for h in out.spike_hits} - tail_labels
        seen["mad_on_tail_row"] += len(out.spike_hits) - sum(
            1 for h in out.spike_hits if h.timestamp in new_labels)
    flushed, state = flush_duration_stuck(state.stuck, BATCH_PROFILE)  # type: ignore[arg-type]
    want_flush = [up._stuck_dict_to_high_detail(a).model_dump() for a in up._WALLCLOCK_DETECTOR.flush(stuck)]
    assert [up._stuck_dict_to_high_detail(a.as_dict()).model_dump() for a in flushed] == want_flush
    assert state.to_carry() == stuck
    seen["stuck"] += len(want_flush)
    return seen


def test_stages_equal_score_portion_portion_by_portion(sources: list) -> None:
    totals: dict[str, int] = {}
    multi_portion_runs = 0
    for name, path, sizes in sources:
        imputed = _imputed(path)
        for size in sizes:
            seen = _run_both(path, size, imputed, json_hop=False)
            multi_portion_runs += seen["portions"] > 1
            for key, n in seen.items():
                totals[key] = totals.get(key, 0) + n
    # Non-vacuous: every stage fired, and boundaries fell inside a fired
    # freeze and inside a spike horizon (a tail row reported once, late).
    assert multi_portion_runs >= len(sources) * 2
    for key in ("oob", "stuck", "stat", "mad", "mad_on_tail_row", "boundary_in_fired_freeze"):
        assert totals[key] > 0, (key, totals)


def test_state_survives_a_json_hop_at_every_boundary(sources: list) -> None:
    """The carry is serialisable: re-loading it each portion changes nothing."""
    picked = [s for s in sources if s[0] in ("dec", "july_spiked", "spike", "oob")]
    for name, path, sizes in picked:
        _run_both(path, sizes[0] if name in ("dec", "july_spiked") else 95, _imputed(path), json_hop=True)


def test_async_message_carries_reproduce_the_worker() -> None:
    """Producer + worker replay: states built from message fields give the worker's output."""
    imputed = _imputed(DEC_CSV)
    producer_stuck = StuckCarry(skip_channels=frozenset(imputed))
    stage_stuck = StuckStageState.initial(imputed)
    prev_norm: list[dict[str, Any]] = []
    prev_raw: list[dict[str, Any]] = []
    prior = 0
    portions = 0
    for normalized, raw_df in _portions(DEC_CSV, 450):
        rows = _normalized_records(normalized) if normalized is not None else []
        raw_rows = _raw_records(raw_df) if not raw_df.empty else []
        message = json.loads(json.dumps({  # what the queue carries
            "rows": rows, "raw_rows": raw_rows, "overlap_rows": prev_norm,
            "stat_carry_rows": prev_raw, "stat_carry_prior_rows": prior,
            "stuck_carry_state": producer_stuck.to_dict()}))
        assert StuckStageState.from_dict(message["stuck_carry_state"]) == stage_stuck
        if normalized is not None:
            up._WALLCLOCK_DETECTOR.detect_chunk(clamp_frame(normalized), producer_stuck)
            _, stage_stuck = duration_stuck_stage(clamp_frame(normalized), stage_stuck, BATCH_PROFILE)
        # Worker side, today.
        worker_stuck = StuckCarry.from_dict(message["stuck_carry_state"])
        frame = pd.DataFrame(message["rows"]) if message["rows"] else None
        overlap = pd.DataFrame(message["overlap_rows"]) if message["overlap_rows"] else pd.DataFrame()
        portion, _ = up.score_portion(
            normalized_df=frame, prev_overlap_df=overlap, raw_rows=message["raw_rows"],
            stat_carry=up.StatCarry(rows=list(message["stat_carry_rows"]),
                                    total_rows=message["stat_carry_prior_rows"]),
            adaptive_ranges=[(0.0, 1.0)] * 4, detector=_ZeroDetector(), seq_len=SEQ_LEN,
            stuck_carry=worker_stuck)
        # Worker side, through the stages.
        state = UploadStreamState(
            rolling_z=RollingZState.from_parts(rows=message["stat_carry_rows"],
                                               total_rows=message["stat_carry_prior_rows"]),
            spike_tail=SpikeTailState.from_frame(overlap, SEQ_LEN - 1),
            stuck=StuckStageState.from_dict(message["stuck_carry_state"]))
        out, after = score_upload_stream(frame, message["raw_rows"], state, CTX, BATCH_PROFILE)
        assert _as_portion(out) == _of_portion(portion)
        assert after.stuck == StuckStageState.from_carry(worker_stuck) == stage_stuck
        if rows:
            prev_norm = rows[-(SEQ_LEN - 1):]
        if raw_rows:
            prior += len(raw_rows)
            prev_raw = (prev_raw + raw_rows)[-_STAT_CARRY_LEN:]
        portions += 1
    assert portions > 3


def test_profile_gates_and_purity() -> None:
    frame = build_corpus()[5].frame  # the OOB case
    hits, scoring = oob_clamp_stage(frame, BATCH_PROFILE)
    assert hits and scoring is not frame and frame["temperature"].max() > scoring["temperature"].max()
    with pytest.raises(ValueError):
        oob_clamp_stage(frame, STREAM_PROFILE)
    with pytest.raises(ValueError):
        spike_lookahead_stage(frame, SpikeTailState(tail_rows=SEQ_LEN - 1), CTX, STREAM_PROFILE)
    off = dataclasses.replace(BATCH_PROFILE, legacy_duration_stuck_enabled=False,
                              legacy_rolling_z_enabled=False)
    stuck = StuckStageState.initial()
    assert duration_stuck_stage(frame, stuck, off) == ([], stuck)
    assert flush_duration_stuck(stuck, off) == ([], stuck)
    z = RollingZState.from_parts(rows=[{"timestamp": "2026-07-01T00:00:00", "temperature": 20.0}])
    assert rolling_z_stage([{"timestamp": "2026-07-01T00:05:00", "temperature": 21.0}], z, off) == ([], z)
    with pytest.raises(ValueError):
        SpikeTailState(tail_rows=2, labels=("a",), values={"temperature": (1.0, 2.0)})

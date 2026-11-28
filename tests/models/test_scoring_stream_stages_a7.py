"""ADR-014 step A7: the upload stream stages reproduce the pre-A7 upload path.

The parity corpus frames and the two committed native fixtures are read the
way ``run_cold_run_upload`` reads them (``_read_chunks``, adapter normalise,
``parse_raw_rows`` / ``_strip_raw_records``), split into portions of several
sizes, and run through ``score_upload_stream`` with its carried state. OOB,
duration-stuck, rolling-z and look-ahead spike outputs and every carried
state must equal ``tests/fixtures/upload_stream_a7_golden.json`` after every
portion, and the end-of-file stuck flush too.

The golden was generated on the unmodified pre-A7-backend code (``main`` at
``1a5061b``), by running today's ``score_portion`` / ``StatCarry`` /
``StuckCarry`` / ``_WALLCLOCK_DETECTOR.flush`` over the same inputs, and
stores a SHA-256 of each portion's outputs and of each carried state (plus
hit counts). Until A7 backend this test compared the stages against those
functions live; they are deleted, so the golden is what is left of that
comparison.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterator

import pandas as pd
import pytest

from src.api.schemas.sensor_data import StatisticalAnomalyDetail, UploadHighAnomalyDetail
from src.api.services import upload_portions as up
from src.api.services.cold_run_upload import _read_chunks, _strip_raw_records
from src.api.services.upload_producer import _normalized_records, _raw_records
from src.config import COLD_RUN_STAT_MIN_CONSECUTIVE, COLD_RUN_STAT_ROLLING_WINDOW, SEQ_LEN
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


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(type(value))


def _digest(obj: Any) -> str:
    """Stable hash of a JSON-like value (NaN spelled 'NaN', sets sorted)."""
    return hashlib.sha256(
        json.dumps(_nan_safe(obj), sort_keys=True, default=_json_default).encode()
    ).hexdigest()


def _stuck_repr(stuck_dict: dict[str, Any]) -> dict[str, Any]:
    out = dict(stuck_dict)
    out["emitted_onsets"] = sorted(out["emitted_onsets"])
    out["skip_channels"] = sorted(out["skip_channels"])
    return out


def _state_repr(stuck: dict[str, Any], rows: list[Any], total_rows: int, columns: Any,
                seen: Any, psychro: Any, labels: list[str], tail: dict[str, list[float]]) -> dict[str, Any]:
    """Carried state in plain values, however it is held (carry objects or stage state)."""
    return {
        "stuck": _stuck_repr(stuck),
        "rolling_z": {"rows": [dict(r) for r in rows], "total_rows": total_rows,
                      "columns": None if columns is None else list(columns),
                      "seen": sorted([t, c] for t, c in seen),
                      "psychro": {ts: list(v) for ts, v in psychro.items()}},
        "tail": {"labels": list(labels), "values": {s: list(v) for s, v in tail.items()} if labels else {}},
    }


_GOLDEN = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "upload_stream_a7_golden.json").read_text()
)


def _tail_values(state: UploadStreamState) -> dict[str, list[float]]:
    return {s: list(v) for s, v in state.spike_tail.values.items()}


def _state_digest(state: UploadStreamState) -> str:
    assert state.stuck is not None
    z = state.rolling_z
    return _digest(_state_repr(
        state.stuck.to_dict(), [dict(r) for r in z.rows], z.total_rows, z.columns, z.seen,
        z.psychro_context, list(state.spike_tail.labels), _tail_values(state)))


def _run_stream(path: Path, key: str, size: int, imputed: list[str], *, json_hop: bool) -> dict[str, int]:
    """Run ``path`` in ``size`` portions through the stages; check the golden; return counts."""
    want_run = _GOLDEN["runs"][f"{key}|{size}"]
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
        got = _as_portion(out)
        want = want_run["portions"][seen["portions"]]
        where = f"{path.name} size={size} portion={seen['portions']}"
        assert [len(got[k]) for k in ("oob", "stuck", "stat", "mad")] == want["n"], where
        assert _digest(got) == want["out"], where
        assert _state_digest(state) == want["state"], where
        if json_hop:
            state = UploadStreamState.from_dict(json.loads(json.dumps(state.to_dict())))
        seen["portions"] += 1
        for key_, n in zip(("oob", "stuck", "stat", "mad"), want["n"]):
            seen[key_] += n
        new_labels = {h.timestamp for h in out.spike_hits} - tail_labels
        seen["mad_on_tail_row"] += len(out.spike_hits) - sum(
            1 for h in out.spike_hits if h.timestamp in new_labels)
    assert seen["portions"] == len(want_run["portions"])
    flushed, final_stuck = flush_duration_stuck(state.stuck, BATCH_PROFILE)  # type: ignore[arg-type]
    flush_dumps = [up._stuck_dict_to_high_detail(a.as_dict()).model_dump() for a in flushed]
    assert len(flush_dumps) == want_run["n_flush"]
    assert _digest(flush_dumps) == want_run["flush"]
    assert _digest(_stuck_repr(final_stuck.to_dict())) == want_run["final_stuck"]
    seen["stuck"] += len(flush_dumps)
    return seen


def test_stages_reproduce_pre_a7_upload_path_portion_by_portion(sources: list) -> None:
    totals: dict[str, int] = {}
    multi_portion_runs = 0
    for name, path, sizes in sources:
        imputed = _imputed(path)
        for size in sizes:
            seen = _run_stream(path, name, size, imputed, json_hop=False)
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
        _run_stream(path, name, sizes[0] if name in ("dec", "july_spiked") else 95, _imputed(path),
                    json_hop=True)


def test_async_message_carries_reproduce_the_worker() -> None:
    """Producer + worker replay: states built from message fields give the pre-A7 worker's output."""
    imputed = _imputed(DEC_CSV)
    want_portions = _GOLDEN["async_dec_450"]
    stage_stuck = StuckStageState.initial(imputed)
    prev_norm: list[dict[str, Any]] = []
    prev_raw: list[dict[str, Any]] = []
    prior = 0
    portions = 0
    for normalized, raw_df in _portions(DEC_CSV, 450):
        want = want_portions[portions]
        rows = _normalized_records(normalized) if normalized is not None else []
        raw_rows = _raw_records(raw_df) if not raw_df.empty else []
        message = json.loads(json.dumps({  # what the queue carries
            "rows": rows, "raw_rows": raw_rows, "overlap_rows": prev_norm,
            "stat_carry_rows": prev_raw, "stat_carry_prior_rows": prior,
            "stuck_carry_state": stage_stuck.to_dict()}))
        assert _digest(_stuck_repr(message["stuck_carry_state"])) == want["message_stuck"]
        assert StuckStageState.from_dict(message["stuck_carry_state"]) == stage_stuck
        if normalized is not None:
            _, stage_stuck = duration_stuck_stage(clamp_frame(normalized), stage_stuck, BATCH_PROFILE)
        assert _digest(_stuck_repr(stage_stuck.to_dict())) == want["producer_stuck_after"]
        # Worker side, through the stages.
        frame = pd.DataFrame(message["rows"]) if message["rows"] else None
        overlap = pd.DataFrame(message["overlap_rows"]) if message["overlap_rows"] else pd.DataFrame()
        state = UploadStreamState(
            rolling_z=RollingZState.from_parts(rows=message["stat_carry_rows"],
                                               total_rows=message["stat_carry_prior_rows"]),
            spike_tail=SpikeTailState.from_frame(overlap, SEQ_LEN - 1),
            stuck=StuckStageState.from_dict(message["stuck_carry_state"]))
        out, after = score_upload_stream(frame, message["raw_rows"], state, CTX, BATCH_PROFILE)
        assert _digest(_as_portion(out)) == want["out"]
        assert after.stuck is not None
        assert _digest(_stuck_repr(after.stuck.to_dict())) == want["worker_stuck_after"]
        assert after.stuck == stage_stuck
        if rows:
            prev_norm = rows[-(SEQ_LEN - 1):]
        if raw_rows:
            prior += len(raw_rows)
            prev_raw = (prev_raw + raw_rows)[-_STAT_CARRY_LEN:]
        portions += 1
    assert portions > 3
    assert portions == len(want_portions)


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

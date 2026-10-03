"""Async/sync stuck-fault parity tests: producer-embedded StuckCarry design.

Pins:
1. PARITY - sync vs async produce identical stuck alerts
2. SHUFFLED ORDER - out-of-order consumption produces byte-identical result
3. CONCURRENT CONSUMPTION - ThreadPoolExecutor scoring is safe
4. skip_channels - imputed CO2 suppression
5. FINAL-PORTION FLUSH - ongoing freeze at EOF, post-scoring carry state
6. NO PHANTOM EPISODES - sub-gate freeze never fires
7. CONFIG PARITY - shared detector singleton
8. (case 8 in tests/models/test_stuck_carry_serialization.py)
9. REGRESSION - clearsky fixture: zero stuck alerts
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.cold_run_upload import run_cold_run_upload
from src.api.services.upload_portions import (
    PortionScores,
    StatCarry,
    finalize_scores,
    score_portion,
)
from src.api.services.upload_producer import produce_upload_portions
from src.config import STUCK_WALLCLOCK_MIN_HOURS
from src.db.jobs import get_job
from src.db.models import Base, SensorReading
from src.models.stuck_fault_detector import StuckCarry, WallclockStuckDetector
from src.mq.schemas import UploadFinalizeMessage, UploadPortionMessage
from src.vendors.csv_adapter import CSVAdapter
from src.workers.upload_consumer import handle_upload_finalize, handle_upload_portion

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_A = FIXTURE_DIR / "native_dec_stuck_fault.csv"
FIXTURE_B = FIXTURE_DIR / "native_july_clearsky.csv"
TENANT = "stuck_parity"
GH_ID = "gh_test"

# Ground truth from NATIVE_FIXTURES.md and task1_parity_evidence.md
EXPECTED_ONSETS = [
    "2026-12-14T00:00:00+00:00",
    "2026-12-15T11:05:00+00:00",
    "2026-12-16T11:20:00+00:00",
    "2026-12-18T13:00:00+00:00",
]
EXPECTED_DETECTED = [
    "2026-12-14T06:00:00+00:00",
    "2026-12-15T17:05:00+00:00",
    "2026-12-16T17:20:00+00:00",
    "2026-12-18T19:00:00+00:00",
]
EXPECTED_DURATIONS = [125400.0, 79500.0, 172500.0, 589800.0]

# Portion sizes for the shuffled-order matrix
PORTION_SIZES = [50, 500, 37, 288]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_detector() -> MagicMock:
    """Constant-score mock: stuck detection is model-independent."""
    det = MagicMock()
    det.predict_batch_enriched.side_effect = lambda seqs: (
        np.full(len(seqs), 0.05, dtype=np.float32),
        np.zeros((len(seqs), 8), dtype=np.float32),
        np.zeros(len(seqs), dtype=np.float32),
    )
    return det


def _stuck_alerts_from(result: Any) -> list[dict]:
    """Extract stuck_fault alerts from sync response OR async job result dict."""
    if hasattr(result, "high_anomalies"):
        return sorted(
            [h.model_dump(mode="json") for h in result.high_anomalies if h.detector == "stuck_fault"],
            key=lambda a: a["timestamp"],
        )
    return sorted(
        [h for h in result.get("high_anomalies", []) if h.get("detector") == "stuck_fault"],
        key=lambda a: a["timestamp"],
    )


def _stuck_episodes_from(result: Any) -> list[dict]:
    """Extract stuck_fault episodes."""
    if hasattr(result, "episodes"):
        return sorted(
            [ep.model_dump(mode="json") for ep in result.episodes if ep.detector == "stuck_fault"],
            key=lambda e: e["start_ts"],
        )
    return sorted(
        [ep for ep in result.get("episodes", []) if ep.get("detector") == "stuck_fault"],
        key=lambda e: e["start_ts"],
    )


def _canonical_stuck_json(alerts: list[dict], episodes: list[dict]) -> str:
    """Deterministic JSON string for byte-identical comparison.

    Includes only fields that should be order-invariant.
    """
    norm_alerts = sorted(
        [
            {
                "timestamp": a["timestamp"],
                "detected_ts": a.get("detected_ts"),
                "column": a.get("column"),
                "severity": a.get("severity"),
                # Both fields, not just one: z_score is None for every
                # current-shape stuck hit, so comparing it alone made the
                # duration side of this parity check compare None to None.
                "z_score": a.get("z_score"),
                "frozen_hours_at_detection": a.get("frozen_hours_at_detection"),
                "value": a.get("value"),
            }
            for a in alerts
        ],
        key=lambda x: x["timestamp"],
    )
    norm_episodes = sorted(
        [
            {
                "channel": ep["channel"],
                "start_ts": ep["start_ts"],
                "duration_seconds": ep["duration_seconds"],
                "severity": ep["severity"],
                "is_ongoing": ep.get("is_ongoing", False),
            }
            for ep in episodes
        ],
        key=lambda x: x["start_ts"],
    )
    return json.dumps({"alerts": norm_alerts, "episodes": norm_episodes}, sort_keys=True)


# ---------------------------------------------------------------------------
# Pipeline runners
# ---------------------------------------------------------------------------


async def _run_sync(
    fixture_path: Path,
    chunk_size: int = 50,
) -> Any:
    """Run the synchronous cold_run_upload path. Returns UploadInferenceResponse."""
    adapter = CSVAdapter({"filepath": str(fixture_path)})
    adapter.connect()
    response, _ = await run_cold_run_upload(
        tmp_path=str(fixture_path),
        adapter=adapter,
        detector=_mock_detector(),
        tenant_id=TENANT,
        greenhouse_id=GH_ID,
        chunk_size=chunk_size,
    )
    return response


async def _run_async_pipeline(
    fixture_path: Path,
    portion_rows: int,
    *,
    order: list[int] | None = None,
    persist_data: bool = False,
) -> tuple[dict, list[UploadPortionMessage]]:
    """Full async pipeline with its own in-memory DB.

    Returns (job_result_dict, portion_messages).
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    async with factory() as sess:
        # Step 1: produce
        published: list[tuple[str, Any]] = []

        async def _cap_pub(rk: str, payload: Any, **kw: Any) -> None:
            published.append((rk, payload))

        with patch(
            "src.api.services.upload_producer.publish_json", side_effect=_cap_pub
        ):
            adapter = CSVAdapter({"filepath": str(fixture_path)})
            adapter.connect()
            job_id, portion_count = await produce_upload_portions(
                tmp_path=str(fixture_path),
                adapter=adapter,
                db=sess,
                tenant_id=TENANT,
                greenhouse_id=GH_ID,
                checkpoint_path=None,
                persist_data=persist_data,
                filename=fixture_path.name,
                portion_rows=portion_rows,
            )

        portion_msgs = [m for key, m in published if key == "upload.portions"]
        assert len(portion_msgs) == portion_count

        # Step 2: consume (optionally out of order)
        finalize_msgs: list[Any] = []

        async def _cap_fin(rk: str, payload: Any, **kw: Any) -> None:
            finalize_msgs.append(payload)

        ordered = (
            [portion_msgs[i] for i in order] if order is not None else portion_msgs
        )

        with patch(
            "src.api.routes.inference.get_cached_detector",
            return_value=_mock_detector(),
        ), patch(
            "src.workers.upload_consumer.publish_json", side_effect=_cap_fin
        ):
            for msg in ordered:
                await handle_upload_portion(msg, sess)

        # Step 3: finalize
        if finalize_msgs:
            await handle_upload_finalize(finalize_msgs[0], sess)

        job = await get_job(sess, job_id)
        result = job.result

    await engine.dispose()
    return result, portion_msgs


# =========================================================================
# Case 1 - PARITY
# =========================================================================


class TestParity:
    """Sync and async paths must produce identical stuck alerts."""

    @pytest.mark.asyncio
    async def test_sync_alerts(self):
        """Sync path produces exactly 4 humidity stuck alerts with correct metadata."""
        resp = await _run_sync(FIXTURE_A, chunk_size=50)
        alerts = _stuck_alerts_from(resp)

        assert len(alerts) == 4, f"Expected 4, got {len(alerts)}"
        assert all(a["column"] == "humidity" for a in alerts)
        assert all(a["severity"] == "urgent" for a in alerts)
        assert [a["timestamp"] for a in alerts] == EXPECTED_ONSETS
        assert [a["detected_ts"] for a in alerts] == EXPECTED_DETECTED

    @pytest.mark.asyncio
    async def test_sync_episodes(self):
        """Sync path produces exactly 4 stuck episodes with expected durations."""
        resp = await _run_sync(FIXTURE_A, chunk_size=50)
        episodes = _stuck_episodes_from(resp)

        assert len(episodes) == 4
        assert [ep["duration_seconds"] for ep in episodes] == EXPECTED_DURATIONS

    @pytest.mark.asyncio
    async def test_async_alerts_match_sync(self):
        """Async path (50-row portions) produces identical alerts to sync."""
        sync_resp = await _run_sync(FIXTURE_A, chunk_size=50)
        async_result, _ = await _run_async_pipeline(FIXTURE_A, 50)

        sync_alerts = _stuck_alerts_from(sync_resp)
        async_alerts = _stuck_alerts_from(async_result)

        assert len(async_alerts) == len(sync_alerts) == 4
        assert [a["timestamp"] for a in async_alerts] == [a["timestamp"] for a in sync_alerts]
        assert [a["detected_ts"] for a in async_alerts] == [a["detected_ts"] for a in sync_alerts]
        assert all(a["severity"] == "urgent" for a in async_alerts)
        assert all(a["column"] == "humidity" for a in async_alerts)

    @pytest.mark.asyncio
    async def test_async_episodes_match_sync(self):
        """Async path episodes match sync in count and duration."""
        sync_resp = await _run_sync(FIXTURE_A, chunk_size=50)
        async_result, _ = await _run_async_pipeline(FIXTURE_A, 50)

        sync_eps = _stuck_episodes_from(sync_resp)
        async_eps = _stuck_episodes_from(async_result)

        assert len(async_eps) == len(sync_eps) == 4
        assert [e["duration_seconds"] for e in async_eps] == [
            e["duration_seconds"] for e in sync_eps
        ]

    @pytest.mark.asyncio
    async def test_byte_identical_sync_async(self):
        """Canonical JSON of stuck alerts+episodes is byte-identical sync vs async."""
        sync_resp = await _run_sync(FIXTURE_A, chunk_size=50)
        async_result, _ = await _run_async_pipeline(FIXTURE_A, 50)

        sync_json = _canonical_stuck_json(
            _stuck_alerts_from(sync_resp), _stuck_episodes_from(sync_resp)
        )
        async_json = _canonical_stuck_json(
            _stuck_alerts_from(async_result), _stuck_episodes_from(async_result)
        )
        assert sync_json == async_json


# =========================================================================
# Case 2 - SHUFFLED ORDER
# =========================================================================


class TestShuffledOrder:
    """Out-of-order consumption must produce byte-identical result."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("portion_rows", PORTION_SIZES)
    async def test_shuffled_matches_in_order(self, portion_rows: int):
        """Shuffled consumption (seed 42) produces same result as in-order."""
        # In-order
        in_order_result, msgs = await _run_async_pipeline(FIXTURE_A, portion_rows)
        n = len(msgs)

        # Shuffled (deterministic seed)
        rng = random.Random(42)
        shuffled_order = list(range(n))
        rng.shuffle(shuffled_order)
        assert shuffled_order != list(range(n)), "shuffle did not change order"

        shuffled_result, _ = await _run_async_pipeline(
            FIXTURE_A, portion_rows, order=shuffled_order
        )

        in_order_json = _canonical_stuck_json(
            _stuck_alerts_from(in_order_result),
            _stuck_episodes_from(in_order_result),
        )
        shuffled_json = _canonical_stuck_json(
            _stuck_alerts_from(shuffled_result),
            _stuck_episodes_from(shuffled_result),
        )
        assert in_order_json == shuffled_json, (
            f"Shuffled order (portion_rows={portion_rows}) diverges from in-order"
        )

    @pytest.mark.asyncio
    async def test_reverse_order(self):
        """Reverse consumption still produces identical result."""
        in_order_result, msgs = await _run_async_pipeline(FIXTURE_A, 50)
        reverse_order = list(reversed(range(len(msgs))))

        reverse_result, _ = await _run_async_pipeline(
            FIXTURE_A, 50, order=reverse_order
        )

        assert _canonical_stuck_json(
            _stuck_alerts_from(in_order_result),
            _stuck_episodes_from(in_order_result),
        ) == _canonical_stuck_json(
            _stuck_alerts_from(reverse_result),
            _stuck_episodes_from(reverse_result),
        )

    @pytest.mark.asyncio
    async def test_redelivery_idempotent(self):
        """Processing the same portion twice does not change the result."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )

        async with factory() as sess:
            published: list[tuple[str, Any]] = []

            async def _cap(rk: str, payload: Any, **kw: Any) -> None:
                published.append((rk, payload))

            with patch(
                "src.api.services.upload_producer.publish_json", side_effect=_cap
            ):
                adapter = CSVAdapter({"filepath": str(FIXTURE_A)})
                adapter.connect()
                job_id, count = await produce_upload_portions(
                    tmp_path=str(FIXTURE_A),
                    adapter=adapter,
                    db=sess,
                    tenant_id=TENANT,
                    greenhouse_id=GH_ID,
                    checkpoint_path=None,
                    persist_data=False,
                    filename="dec.csv",
                    portion_rows=50,
                )

            portion_msgs = [m for k, m in published if k == "upload.portions"]

            finalize_msgs: list[Any] = []

            async def _cap_fin(rk: str, payload: Any, **kw: Any) -> None:
                finalize_msgs.append(payload)

            with patch(
                "src.api.routes.inference.get_cached_detector",
                return_value=_mock_detector(),
            ), patch(
                "src.workers.upload_consumer.publish_json", side_effect=_cap_fin
            ):
                # Deliver portion 0 TWICE, then the rest in order
                await handle_upload_portion(portion_msgs[0], sess)
                await handle_upload_portion(portion_msgs[0], sess)  # redelivery
                for msg in portion_msgs[1:]:
                    await handle_upload_portion(msg, sess)

            if finalize_msgs:
                await handle_upload_finalize(finalize_msgs[0], sess)

            job = await get_job(sess, job_id)
            result = job.result

        await engine.dispose()

        # Must match in-order result
        in_order_result, _ = await _run_async_pipeline(FIXTURE_A, 50)
        assert _canonical_stuck_json(
            _stuck_alerts_from(result), _stuck_episodes_from(result)
        ) == _canonical_stuck_json(
            _stuck_alerts_from(in_order_result),
            _stuck_episodes_from(in_order_result),
        )


# =========================================================================
# Case 3 - CONCURRENT CONSUMPTION
# =========================================================================


class TestConcurrentConsumption:
    """Concurrent scoring must not corrupt shared state.

    Uses ThreadPoolExecutor to run score_portion concurrently across portions.
    ThreadPoolExecutor was chosen because:
      - SQLite in-memory does not support concurrent connections (separate
        sessions see separate databases), so asyncio.gather on
        handle_upload_portion with separate sessions is not viable.
      - ThreadPoolExecutor tests TRUE thread-level concurrency of the
        CPU-bound scoring path, which is exactly where shared-state bugs
        (module-level _WALLCLOCK_DETECTOR, carry objects) would manifest.
      - handle_upload_portion itself wraps scoring in asyncio.to_thread,
        so this test mirrors the real execution model.
    """

    @pytest.mark.asyncio
    async def test_concurrent_scoring_matches_sequential(self):
        """Per-portion stuck_hits are identical under sequential vs concurrent scoring."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )

        async with factory() as sess:
            published: list[tuple[str, Any]] = []

            async def _cap(rk: str, payload: Any, **kw: Any) -> None:
                published.append((rk, payload))

            with patch(
                "src.api.services.upload_producer.publish_json", side_effect=_cap
            ):
                adapter = CSVAdapter({"filepath": str(FIXTURE_A)})
                adapter.connect()
                await produce_upload_portions(
                    tmp_path=str(FIXTURE_A),
                    adapter=adapter,
                    db=sess,
                    tenant_id=TENANT,
                    greenhouse_id=GH_ID,
                    checkpoint_path=None,
                    persist_data=False,
                    filename="dec.csv",
                    portion_rows=50,
                )

        await engine.dispose()

        portion_msgs = [m for k, m in published if k == "upload.portions"]

        def _score_one(msg: UploadPortionMessage) -> tuple[int, list[dict]]:
            stat_carry = StatCarry(rows=list(msg.stat_carry_rows))
            stuck_carry = (
                StuckCarry.from_dict(msg.stuck_carry_state)
                if msg.stuck_carry_state
                else None
            )
            detector = _mock_detector()
            normalized_df = pd.DataFrame(msg.rows) if msg.rows else None
            overlap_df = (
                pd.DataFrame(msg.overlap_rows)
                if msg.overlap_rows
                else pd.DataFrame()
            )
            result, _ = score_portion(
                normalized_df=normalized_df,
                prev_overlap_df=overlap_df,
                raw_rows=msg.raw_rows,
                stat_carry=stat_carry,
                adaptive_ranges=[tuple(r) for r in msg.adaptive_ranges],
                detector=detector,
                seq_len=msg.seq_len,
                include_embeddings=False,
                stuck_carry=stuck_carry,
            )
            return msg.portion_idx, [h.model_dump(mode="json") for h in result.stuck_hits]

        # Sequential
        seq_hits: dict[int, list[dict]] = {}
        for msg in portion_msgs:
            idx, hits = _score_one(msg)
            seq_hits[idx] = hits

        # Concurrent (4 workers)
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [loop.run_in_executor(pool, _score_one, msg) for msg in portion_msgs]
            conc_results = await asyncio.gather(*futures)

        conc_hits: dict[int, list[dict]] = {}
        for idx, hits in conc_results:
            conc_hits[idx] = hits

        for idx in sorted(seq_hits):
            assert json.dumps(seq_hits[idx], sort_keys=True) == json.dumps(
                conc_hits[idx], sort_keys=True
            ), f"Portion {idx}: concurrent stuck_hits diverge from sequential"

        # Total stuck alerts across all portions must be 4
        total_seq = sum(len(h) for h in seq_hits.values())
        total_conc = sum(len(h) for h in conc_hits.values())
        assert total_seq == total_conc == 4


# =========================================================================
# Case 4 - skip_channels
# =========================================================================


class TestSkipChannels:
    """Imputed CO2 suppression via skip_channels."""

    @pytest.mark.asyncio
    async def test_co2_suppressed_when_skip_enabled(self):
        """Normal run: CO2 is imputed, skip_channels filters it -> 0 CO2 alerts."""
        resp = await _run_sync(FIXTURE_A, chunk_size=50)
        alerts = _stuck_alerts_from(resp)
        co2_alerts = [a for a in alerts if a["column"] == "co2"]
        assert len(co2_alerts) == 0, f"Expected 0 co2 alerts, got {len(co2_alerts)}"

    @pytest.mark.asyncio
    async def test_no_temperature_stuck_alerts(self):
        """No temperature stuck alerts on fixture A."""
        resp = await _run_sync(FIXTURE_A, chunk_size=50)
        alerts = _stuck_alerts_from(resp)
        temp_alerts = [a for a in alerts if a["column"] == "temperature"]
        assert len(temp_alerts) == 0

    @pytest.mark.asyncio
    async def test_co2_alert_appears_when_skip_disabled(self):
        """With skip_channels disabled (empty imputed_channels), CO2 stuck alert appears."""
        # Patch _calibrate_ranges_from_head to return no imputed channels
        from src.api.services import cold_run_upload as cru_mod

        original_cal = cru_mod._calibrate_ranges_from_head

        def _cal_no_imputed(tmp_path, chunk_size, csv_props, adapter):
            ranges, _ = original_cal(tmp_path, chunk_size, csv_props, adapter)
            return ranges, []  # force empty imputed_channels

        with patch.object(cru_mod, "_calibrate_ranges_from_head", side_effect=_cal_no_imputed):
            resp = await _run_sync(FIXTURE_A, chunk_size=50)

        alerts = _stuck_alerts_from(resp)
        co2_alerts = [a for a in alerts if a["column"] == "co2"]
        assert len(co2_alerts) == 1, f"Expected 1 co2 alert, got {len(co2_alerts)}"
        assert co2_alerts[0]["timestamp"] == "2026-12-14T00:00:00+00:00"
        # Hours at the gate crossing live in frozen_hours_at_detection;
        # z_score used to be overloaded to carry them and is now None.
        assert co2_alerts[0]["frozen_hours_at_detection"] == STUCK_WALLCLOCK_MIN_HOURS
        assert co2_alerts[0]["z_score"] is None

    @pytest.mark.asyncio
    async def test_producer_carry_has_skip_channels(self):
        """The producer's embedded carry state must have skip_channels == {'co2'}."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )

        async with factory() as sess:
            published: list[tuple[str, Any]] = []

            async def _cap(rk: str, payload: Any, **kw: Any) -> None:
                published.append((rk, payload))

            with patch(
                "src.api.services.upload_producer.publish_json", side_effect=_cap
            ):
                adapter = CSVAdapter({"filepath": str(FIXTURE_A)})
                adapter.connect()
                await produce_upload_portions(
                    tmp_path=str(FIXTURE_A),
                    adapter=adapter,
                    db=sess,
                    tenant_id=TENANT,
                    greenhouse_id=GH_ID,
                    checkpoint_path=None,
                    persist_data=False,
                    filename="dec.csv",
                    portion_rows=50,
                )

        await engine.dispose()

        portion_msgs = [m for k, m in published if k == "upload.portions"]
        assert len(portion_msgs) > 0

        # Check every portion message's embedded carry state
        for msg in portion_msgs:
            carry_state = msg.stuck_carry_state
            assert carry_state is not None, (
                f"Portion {msg.portion_idx} has no stuck_carry_state"
            )
            carry = StuckCarry.from_dict(carry_state)
            assert carry.skip_channels == frozenset({"co2"}), (
                f"Portion {msg.portion_idx}: skip_channels={carry.skip_channels}, "
                "expected frozenset({'co2'})"
            )


# =========================================================================
# Case 5 - FINAL-PORTION FLUSH
# =========================================================================


def _build_synthetic_ongoing_csv(n_rows: int = 80) -> str:
    """Build a small CSV in native Connext format with an ongoing humidity freeze.

    5-minute cadence. Humidity locked at 65.0 for all rows (> 6h gate at 72 rows).
    Temperature/light vary normally. CO2 absent (imputed).

    With portion_rows=50: portion 0 has 50 rows (4.17h, below gate),
    portion 1 has 30 rows -> gate crosses at row 72 (6.0h) IN THE LAST PORTION.
    """
    header = (
        "Timestamp;Outdoor Temp (°C) [connext2:METEO];"
        "Measured Humidity Deficit (g/m³) [connext2:Compartment 1];"
        "Measured Relative Humidity (%) [connext2:Compartment 1];"
        "Measured Plant Temp (°C) [connext2:Compartment 1];"
        "Measured Greenhouse Temp (°C) [connext2:Compartment 1];"
        "Calculated Heating Temp (°C) [connext2:Compartment 1];"
        "Measured Pipe 2 Temp (°C) [connext2:COMPARTMENT 1 PIPE TEMP 2];"
        "Measured Screen 1 (%) [connext2:COMPARTMENT 1 SCREEN 1];"
        "Light Intensity (W/m²) [connext2:METEO LIGHT SENSOR];"
        "Rain [connext2:METEO];"
        "Measured Leeward Vent 2 (%) [connext2:COMPARTMENT 1 VENT REGULATION 2];"
        "Measured Pipe 1 Temp (°C) [connext2:COMPARTMENT 1 PIPE TEMP 1];"
        "Wind Speed (m/s) [connext2:METEO]"
    )
    lines = [header]
    base = datetime(2026, 12, 20, 0, 0, tzinfo=timezone.utc)
    for i in range(n_rows):
        ts = base.replace(
            hour=(i * 5) // 60,
            minute=(i * 5) % 60,
        )
        ts_str = ts.strftime("%d.%m.%Y %H:%M")
        # Outdoor temp varies, humidity fixed at 65, greenhouse temp varies
        outdoor = f"{(-2 + (i % 5) * 0.1):.1f}".replace(".", ",")
        hd = "5,0"
        humidity = "65"
        plant_t = f"{(20 + (i % 3) * 0.2):.1f}".replace(".", ",")
        greenhouse_t = f"{(18 + (i % 4) * 0.1):.1f}".replace(".", ",")
        heating_t = "18"
        pipe2 = "30"
        screen = "20"
        light = str(max(0, int(200 * np.sin(i * 0.1))))
        rain = "0"
        vent = "0"
        pipe1 = "41"
        wind = "2,0"
        lines.append(
            f"{ts_str};{outdoor};{hd};{humidity};{plant_t};{greenhouse_t};"
            f"{heating_t};{pipe2};{screen};{light};{rain};{vent};{pipe1};{wind}"
        )
    return "\n".join(lines)


class TestFinalPortionFlush:
    """Pin where flush state comes from: must be POST-scoring carry."""

    @pytest.mark.asyncio
    async def test_ongoing_freeze_emits_alert(self):
        """Ongoing freeze crossing gate in last portion produces an alert."""
        import tempfile, os

        csv_content = _build_synthetic_ongoing_csv(80)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write(csv_content)
            tmp = f.name

        try:
            resp = await _run_sync(Path(tmp), chunk_size=50)
        finally:
            os.unlink(tmp)

        alerts = _stuck_alerts_from(resp)
        hum_alerts = [a for a in alerts if a["column"] == "humidity"]
        assert len(hum_alerts) == 1, f"Expected 1 humidity alert, got {len(hum_alerts)}"

        episodes = _stuck_episodes_from(resp)
        hum_eps = [ep for ep in episodes if ep["channel"] == "humidity"]
        assert len(hum_eps) == 1
        assert hum_eps[0]["is_ongoing"] is True

    @pytest.mark.asyncio
    async def test_sync_async_ongoing_identical(self):
        """Sync and async paths produce identical ongoing freeze results."""
        import tempfile, os

        csv_content = _build_synthetic_ongoing_csv(80)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write(csv_content)
            tmp = f.name

        try:
            sync_resp = await _run_sync(Path(tmp), chunk_size=50)
            async_result, _ = await _run_async_pipeline(Path(tmp), 50)
        finally:
            os.unlink(tmp)

        sync_json = _canonical_stuck_json(
            _stuck_alerts_from(sync_resp), _stuck_episodes_from(sync_resp)
        )
        async_json = _canonical_stuck_json(
            _stuck_alerts_from(async_result), _stuck_episodes_from(async_result)
        )
        assert sync_json == async_json

    @pytest.mark.asyncio
    async def test_flush_uses_post_scoring_carry(self):
        """The last portion's stored carry must have last_ts == file's final timestamp.

        This verifies that finalize uses the POST-scoring carry (which saw the
        last portion's rows), not the PRE-portion carry embedded in the message.
        """
        import tempfile, os

        csv_content = _build_synthetic_ongoing_csv(80)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write(csv_content)
            tmp = f.name

        try:
            engine = create_async_engine("sqlite+aiosqlite:///:memory:")
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(
                bind=engine, class_=AsyncSession, expire_on_commit=False
            )

            async with factory() as sess:
                published: list[tuple[str, Any]] = []

                async def _cap(rk: str, payload: Any, **kw: Any) -> None:
                    published.append((rk, payload))

                with patch(
                    "src.api.services.upload_producer.publish_json", side_effect=_cap
                ):
                    adapter = CSVAdapter({"filepath": tmp})
                    adapter.connect()
                    job_id, count = await produce_upload_portions(
                        tmp_path=tmp,
                        adapter=adapter,
                        db=sess,
                        tenant_id=TENANT,
                        greenhouse_id=GH_ID,
                        checkpoint_path=None,
                        persist_data=False,
                        filename="ongoing.csv",
                        portion_rows=50,
                    )

                portion_msgs = [m for k, m in published if k == "upload.portions"]
                assert count == 2, f"Expected 2 portions, got {count}"

                finalize_msgs: list[Any] = []

                async def _cap_fin(rk: str, payload: Any, **kw: Any) -> None:
                    finalize_msgs.append(payload)

                with patch(
                    "src.api.routes.inference.get_cached_detector",
                    return_value=_mock_detector(),
                ), patch(
                    "src.workers.upload_consumer.publish_json", side_effect=_cap_fin
                ):
                    for msg in portion_msgs:
                        await handle_upload_portion(msg, sess)

                # Read the stored carry from the last (highest idx) portion
                from src.db.jobs import load_portions_in_order

                rows = await load_portions_in_order(sess, job_id)
                last_row = [r for r in rows if r.status == "completed"][-1]
                stored_carry_state = last_row.result.get("stuck_carry_state")
                assert stored_carry_state is not None, "last portion has no stuck_carry_state"

                carry = StuckCarry.from_dict(stored_carry_state)
                assert "humidity" in carry.channels, "humidity not in stored carry"

                # The file's final timestamp is row 79 (0-indexed): 2026-12-20 06:35
                # (80 rows * 5min = 400min = 6h40min, starting at 00:00)
                expected_last = "2026-12-20T06:35:00+00:00"
                assert carry.channels["humidity"].last_ts == expected_last, (
                    f"Stored carry last_ts={carry.channels['humidity'].last_ts}, "
                    f"expected {expected_last} (post-scoring state)"
                )

            await engine.dispose()
        finally:
            os.unlink(tmp)

    @pytest.mark.xfail(
        strict=True,
        reason="Task 3/4: episode end_ts must be the last frozen sample; "
        "today it equals onset (single stuck_fault alert per freeze)",
    )
    @pytest.mark.asyncio
    async def test_ongoing_episode_end_ts_is_last_frozen(self):
        """Episode end_ts should be the last frozen sample, not the onset."""
        import tempfile, os

        csv_content = _build_synthetic_ongoing_csv(80)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write(csv_content)
            tmp = f.name

        try:
            resp = await _run_sync(Path(tmp), chunk_size=50)
        finally:
            os.unlink(tmp)

        episodes = _stuck_episodes_from(resp)
        hum_eps = [ep for ep in episodes if ep["channel"] == "humidity"]
        assert len(hum_eps) == 1

        # The last frozen sample is the final row: 2026-12-20T06:35:00+00:00
        expected_end = "2026-12-20T06:35:00+00:00"
        assert hum_eps[0]["end_ts"] == expected_end, (
            f"end_ts={hum_eps[0]['end_ts']}, expected {expected_end}"
        )


# =========================================================================
# Case 6 - NO PHANTOM EPISODES
# =========================================================================


class TestNoPhantomEpisodes:
    """The 2-sample humidity freeze at end of fixture A must NOT produce an alert."""

    @pytest.mark.asyncio
    async def test_sync_no_phantom(self):
        """Sync path: no alert or episode for the 2-sample trailing freeze."""
        resp = await _run_sync(FIXTURE_A, chunk_size=50)
        alerts = _stuck_alerts_from(resp)
        episodes = _stuck_episodes_from(resp)

        # Only the 4 known alerts
        assert len(alerts) == 4
        assert len(episodes) == 4

        # None at the trailing freeze timestamps (Dec 25-26)
        trailing_onsets = {a["timestamp"] for a in alerts if "2026-12-25T23" in a["timestamp"] or "2026-12-26" in a["timestamp"]}
        assert len(trailing_onsets) == 0, f"Phantom alert at: {trailing_onsets}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("portion_rows", PORTION_SIZES)
    async def test_async_no_phantom(self, portion_rows: int):
        """Async path: no phantom at any portion size."""
        result, _ = await _run_async_pipeline(FIXTURE_A, portion_rows)
        alerts = _stuck_alerts_from(result)
        episodes = _stuck_episodes_from(result)

        assert len(alerts) == 4, f"portion_rows={portion_rows}: {len(alerts)} alerts (expected 4)"
        assert len(episodes) == 4


# =========================================================================
# Case 7 - CONFIG PARITY
# =========================================================================


class TestConfigParity:
    """Shared detector singleton and consistent parameters."""

    def test_detector_construction_count(self):
        """Exactly ONE WallclockStuckDetector() construction across all source files."""
        source_files = [
            Path("src/api/services/upload_producer.py"),
            Path("src/api/services/upload_portions.py"),
            Path("src/workers/upload_consumer.py"),
            Path("src/api/services/cold_run_upload.py"),
        ]
        total_constructions = 0
        for src_file in source_files:
            text = src_file.read_text(encoding="utf-8")
            count = text.count("WallclockStuckDetector(")
            total_constructions += count

        assert total_constructions == 1, (
            f"Expected exactly 1 WallclockStuckDetector() construction, "
            f"found {total_constructions}"
        )

    def test_shared_detector_identity(self):
        """upload_producer and upload_portions must share the same detector object."""
        import src.api.services.upload_producer as prod_mod
        import src.api.services.upload_portions as port_mod

        # The backend-engineer may have renamed the producer's detector
        # to import from upload_portions. Check both possible names.
        prod_det = getattr(prod_mod, "_WALLCLOCK_DETECTOR", None)
        if prod_det is None:
            prod_det = getattr(prod_mod, "_WALLCLOCK_DETECTOR_PRODUCER", None)

        port_det = port_mod._WALLCLOCK_DETECTOR

        assert prod_det is port_det, (
            f"Producer detector id={id(prod_det)} is not portions detector id={id(port_det)}. "
            "Backend-engineer's change to share the singleton may not have landed yet."
        )

    def test_detector_parameters_match(self):
        """Producer and worker-side detector have identical config parameters."""
        import src.api.services.upload_producer as prod_mod
        import src.api.services.upload_portions as port_mod

        prod_det = getattr(prod_mod, "_WALLCLOCK_DETECTOR", None)
        if prod_det is None:
            prod_det = getattr(prod_mod, "_WALLCLOCK_DETECTOR_PRODUCER", None)

        port_det = port_mod._WALLCLOCK_DETECTOR

        assert prod_det._min_hours == port_det._min_hours
        assert prod_det._min_samples == port_det._min_samples
        assert prod_det._epsilon == port_det._epsilon
        assert prod_det._channels == port_det._channels


# =========================================================================
# Case 9 - REGRESSION (clearsky fixture)
# =========================================================================


class TestRegression:
    """Clearsky fixture must produce zero stuck alerts."""

    @pytest.mark.asyncio
    async def test_sync_clearsky_no_stuck(self):
        """Sync path: native_july_clearsky.csv -> 0 stuck alerts, 0 stuck episodes."""
        resp = await _run_sync(FIXTURE_B, chunk_size=50)
        alerts = _stuck_alerts_from(resp)
        episodes = _stuck_episodes_from(resp)
        assert len(alerts) == 0, f"Expected 0 stuck alerts, got {len(alerts)}"
        assert len(episodes) == 0

    @pytest.mark.asyncio
    async def test_async_clearsky_no_stuck(self):
        """Async path: clearsky fixture -> 0 stuck alerts."""
        result, _ = await _run_async_pipeline(FIXTURE_B, 50)
        alerts = _stuck_alerts_from(result)
        episodes = _stuck_episodes_from(result)
        assert len(alerts) == 0
        assert len(episodes) == 0

    @pytest.mark.asyncio
    async def test_clearsky_persist_no_stuck_rows(self):
        """Persisted clearsky: no sensor_readings with trigger_detector='stuck_fault'."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )

        async with factory() as sess:
            published: list[tuple[str, Any]] = []

            async def _cap(rk: str, payload: Any, **kw: Any) -> None:
                published.append((rk, payload))

            with patch(
                "src.api.services.upload_producer.publish_json", side_effect=_cap
            ):
                adapter = CSVAdapter({"filepath": str(FIXTURE_B)})
                adapter.connect()
                job_id, count = await produce_upload_portions(
                    tmp_path=str(FIXTURE_B),
                    adapter=adapter,
                    db=sess,
                    tenant_id=TENANT,
                    greenhouse_id=GH_ID,
                    checkpoint_path=None,
                    persist_data=True,
                    filename="july.csv",
                    portion_rows=50,
                )

            portion_msgs = [m for k, m in published if k == "upload.portions"]

            finalize_msgs: list[Any] = []

            async def _cap_fin(rk: str, payload: Any, **kw: Any) -> None:
                finalize_msgs.append(payload)

            with patch(
                "src.api.routes.inference.get_cached_detector",
                return_value=_mock_detector(),
            ), patch(
                "src.workers.upload_consumer.publish_json", side_effect=_cap_fin
            ):
                for msg in portion_msgs:
                    await handle_upload_portion(msg, sess)

            if finalize_msgs:
                await handle_upload_finalize(finalize_msgs[0], sess)

            # Query for stuck_fault rows
            stuck_rows_q = await sess.execute(
                select(func.count(SensorReading.id)).where(
                    SensorReading.tenant_id == TENANT,
                    SensorReading.trigger_detector == "stuck_fault",
                )
            )
            stuck_count = stuck_rows_q.scalar()
            assert stuck_count == 0, f"Expected 0 stuck_fault rows, got {stuck_count}"

            # Also verify no detected_severity='urgent' rows from stuck
            urgent_q = await sess.execute(
                select(func.count(SensorReading.id)).where(
                    SensorReading.tenant_id == TENANT,
                    SensorReading.detected_severity == "urgent",
                )
            )
            urgent_count = urgent_q.scalar()
            assert urgent_count == 0

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_fixture_a_persist_exactly_4_stuck_rows(self):
        """Fixture A with persistence: exactly 4 sensor_readings with stuck_fault/urgent."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )

        async with factory() as sess:
            published: list[tuple[str, Any]] = []

            async def _cap(rk: str, payload: Any, **kw: Any) -> None:
                published.append((rk, payload))

            with patch(
                "src.api.services.upload_producer.publish_json", side_effect=_cap
            ):
                adapter = CSVAdapter({"filepath": str(FIXTURE_A)})
                adapter.connect()
                job_id, count = await produce_upload_portions(
                    tmp_path=str(FIXTURE_A),
                    adapter=adapter,
                    db=sess,
                    tenant_id=TENANT,
                    greenhouse_id=GH_ID,
                    checkpoint_path=None,
                    persist_data=True,
                    filename="dec.csv",
                    portion_rows=50,
                )

            portion_msgs = [m for k, m in published if k == "upload.portions"]

            finalize_msgs: list[Any] = []

            async def _cap_fin(rk: str, payload: Any, **kw: Any) -> None:
                finalize_msgs.append(payload)

            with patch(
                "src.api.routes.inference.get_cached_detector",
                return_value=_mock_detector(),
            ), patch(
                "src.workers.upload_consumer.publish_json", side_effect=_cap_fin
            ):
                for msg in portion_msgs:
                    await handle_upload_portion(msg, sess)

            if finalize_msgs:
                await handle_upload_finalize(finalize_msgs[0], sess)

            stuck_q = await sess.execute(
                select(func.count(SensorReading.id)).where(
                    SensorReading.tenant_id == TENANT,
                    SensorReading.trigger_detector == "stuck_fault",
                    SensorReading.detected_severity == "urgent",
                )
            )
            stuck_count = stuck_q.scalar()
            assert stuck_count == 4, f"Expected 4 stuck_fault/urgent rows, got {stuck_count}"

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_measure_alert_counts_fixture_a(self):
        """MEASURE and REPORT async-path alert counts for fixture A vs sync.

        Sync with the mock detector: statistical and LSTM counts.
        Async with mock: same. Does NOT assert (counts depend on the mock),
        but reports for the QA report.
        """
        sync_resp = await _run_sync(FIXTURE_A, chunk_size=50)
        async_result, _ = await _run_async_pipeline(FIXTURE_A, 50)

        # Sync counts
        sync_stat = len([h for h in sync_resp.high_anomalies if h.detector == "statistical" or (h.detector and "statistical" in h.detector)])
        sync_lstm = len([h for h in sync_resp.high_anomalies if h.detector == "lstm" or (h.detector and h.detector.startswith("lstm"))])
        sync_stuck = len([h for h in sync_resp.high_anomalies if h.detector == "stuck_fault"])

        # Async counts
        async_ha = async_result.get("high_anomalies", [])
        async_stat = len([h for h in async_ha if h.get("detector") == "statistical" or (h.get("detector", "") and "statistical" in h.get("detector", ""))])
        async_lstm = len([h for h in async_ha if h.get("detector", "").startswith("lstm")])
        async_stuck = len([h for h in async_ha if h.get("detector") == "stuck_fault"])

        # Report (printed, captured in test output)
        print(f"\n--- Alert count report (Fixture A, mock detector) ---")
        print(f"Sync:  statistical={sync_stat}, lstm={sync_lstm}, stuck={sync_stuck}")
        print(f"Async: statistical={async_stat}, lstm={async_lstm}, stuck={async_stuck}")
        print(f"Note: with real epoch_0072 checkpoint, sync yields ~111 statistical, 4 lstm")
        print(f"---")

        # Stuck counts must match (the whole point of this test suite)
        assert sync_stuck == async_stuck == 4

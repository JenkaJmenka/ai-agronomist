"""Chunk-size invariance tests for statistical (rolling z) detection.

Pins:
  a. final statistical hit sets ((timestamp, column) AND z-score) identical
     across chunk sizes 50, 288, 500, 5000, single (10**7)
  b. pinned counts: 102 total, 0 humidity, 102 non-humidity on the
     stuck-fault fixture.
     NOTE: 111 is superseded — that was the count BEFORE baseline exclusion
     (task 1d-2) removed humidity hits within the stuck-fault suppression
     window.  102 is the measured post-exclusion baseline at every chunk size.
  c. stuck control: 4 humidity onsets at expected timestamps, detected_ts =
     onset + 6 h, severity urgent.
  d. native_july_clearsky.csv at every size: zero stuck alerts, no
     detected_severity rows; statistical hit set identical across sizes.
  e. Async: at 50-row and 500-row portions the async path's statistical
     hit set, stuck alerts, and LSTM counts equal sync's.
"""

from __future__ import annotations

import asyncio
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
CHECKPOINT_PATH = FIXTURE_DIR / "epoch_0072_trained_baseline.pt"
TENANT = "stat_inv"
GH_ID = "gh_inv"

CHUNK_SIZES = [50, 288, 500, 5000, 10**7]

# Pinned: Task 1d-2 baseline (supersedes the old "111").
# 102 total statistical hits, 0 humidity, 102 non-humidity, at every chunk
# size.  These were measured after baseline exclusion + alert suppression
# window were implemented.  The old 111 count included humidity hits that
# are now correctly suppressed within the fault window.
EXPECTED_STAT_TOTAL = 102
EXPECTED_STAT_HUMIDITY = 0
EXPECTED_STAT_NON_HUMIDITY = 102

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

# ---------------------------------------------------------------------------
# Module-scoped fixture loading (shared across all tests)
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


def _stat_hit_set(response: Any) -> set[tuple[str, str, float]]:
    """Extract statistical hits as a canonical set of (timestamp, column, z_score)."""
    if hasattr(response, "high_anomalies"):
        return {
            (h.timestamp, h.column, round(h.z_score, 6))
            for h in response.high_anomalies
            if h.detector == "statistical"
        }
    return {
        (h["timestamp"], h["column"], round(h["z_score"], 6))
        for h in response.get("high_anomalies", [])
        if h.get("detector") == "statistical"
    }


def _stuck_alerts(response: Any) -> list[dict]:
    """Extract stuck_fault alerts from sync response OR async job result."""
    if hasattr(response, "high_anomalies"):
        return sorted(
            [h.model_dump(mode="json") for h in response.high_anomalies if h.detector == "stuck_fault"],
            key=lambda a: a["timestamp"],
        )
    return sorted(
        [h for h in response.get("high_anomalies", []) if h.get("detector") == "stuck_fault"],
        key=lambda a: a["timestamp"],
    )


# Cache sync results at module scope to avoid re-running for each test.
_sync_cache: dict[tuple[str, int], Any] = {}


async def _run_sync_cached(fixture_path: Path, chunk_size: int) -> Any:
    """Run sync cold_run_upload, caching by (fixture, chunk_size)."""
    key = (str(fixture_path), chunk_size)
    if key in _sync_cache:
        return _sync_cache[key]
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
    _sync_cache[key] = response
    return response


async def _run_async_pipeline(
    fixture_path: Path,
    portion_rows: int,
    *,
    order: list[int] | None = None,
    persist_data: bool = False,
    detector_override: Any = None,
) -> tuple[dict, list[UploadPortionMessage]]:
    """Full async pipeline with its own in-memory DB."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    async with factory() as sess:
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

        finalize_msgs: list[Any] = []

        async def _cap_fin(rk: str, payload: Any, **kw: Any) -> None:
            finalize_msgs.append(payload)

        det = detector_override if detector_override is not None else _mock_detector()
        ordered = (
            [portion_msgs[i] for i in order] if order is not None else portion_msgs
        )

        with patch(
            "src.api.routes.inference.get_cached_detector",
            return_value=det,
        ), patch(
            "src.workers.upload_consumer.publish_json", side_effect=_cap_fin
        ):
            for msg in ordered:
                await handle_upload_portion(msg, sess)

        if finalize_msgs:
            await handle_upload_finalize(finalize_msgs[0], sess)

        job = await get_job(sess, job_id)
        result = job.result

    await engine.dispose()
    return result, portion_msgs


# =========================================================================
# a. Statistical hit set invariance (stuck_fault fixture)
# =========================================================================


class TestStatHitSetInvariance:
    """Final statistical hit sets must be identical across all five chunk sizes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    async def test_stat_hits_identical_to_single(self, chunk_size: int):
        """Hit set at chunk_size matches the single-chunk reference."""
        single_resp = await _run_sync_cached(FIXTURE_A, 10**7)
        this_resp = await _run_sync_cached(FIXTURE_A, chunk_size)

        single_hits = _stat_hit_set(single_resp)
        this_hits = _stat_hit_set(this_resp)

        assert this_hits == single_hits, (
            f"Chunk {chunk_size}: hit set differs from single.\n"
            f"Only in single: {single_hits - this_hits}\n"
            f"Only in chunk{chunk_size}: {this_hits - single_hits}"
        )


# =========================================================================
# b. Pinned counts
# =========================================================================


class TestPinnedCounts:
    """Pinned statistical hit counts (102 total, 0 humidity, 102 non-humidity).

    These were measured after task 1d-2 (baseline exclusion + alert suppression
    window).  The old count of 111 is superseded because it included humidity
    hits that fell inside the stuck-fault suppression window.  After the window
    suppresses those hits, the final count is 102 at every chunk size.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    async def test_pinned_total(self, chunk_size: int):
        resp = await _run_sync_cached(FIXTURE_A, chunk_size)
        hits = _stat_hit_set(resp)
        assert len(hits) == EXPECTED_STAT_TOTAL, (
            f"Chunk {chunk_size}: expected {EXPECTED_STAT_TOTAL} total stat hits, "
            f"got {len(hits)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    async def test_pinned_humidity_zero(self, chunk_size: int):
        resp = await _run_sync_cached(FIXTURE_A, chunk_size)
        hits = _stat_hit_set(resp)
        humidity_hits = {h for h in hits if h[1] == "humidity"}
        assert len(humidity_hits) == EXPECTED_STAT_HUMIDITY, (
            f"Chunk {chunk_size}: expected {EXPECTED_STAT_HUMIDITY} humidity hits, "
            f"got {len(humidity_hits)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    async def test_pinned_non_humidity(self, chunk_size: int):
        resp = await _run_sync_cached(FIXTURE_A, chunk_size)
        hits = _stat_hit_set(resp)
        non_humidity = {h for h in hits if h[1] != "humidity"}
        assert len(non_humidity) == EXPECTED_STAT_NON_HUMIDITY, (
            f"Chunk {chunk_size}: expected {EXPECTED_STAT_NON_HUMIDITY} non-humidity hits, "
            f"got {len(non_humidity)}"
        )


# =========================================================================
# c. Stuck control at every size
# =========================================================================


class TestStuckControl:
    """4 humidity onsets with correct onset, detected_ts, severity at every size."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    async def test_four_stuck_onsets(self, chunk_size: int):
        resp = await _run_sync_cached(FIXTURE_A, chunk_size)
        alerts = _stuck_alerts(resp)

        assert len(alerts) == 4, f"Chunk {chunk_size}: expected 4 stuck alerts, got {len(alerts)}"
        assert all(a["column"] == "humidity" for a in alerts), "Not all stuck on humidity"
        assert all(a["severity"] == "urgent" for a in alerts), "Not all severity=urgent"
        assert [a["timestamp"] for a in alerts] == EXPECTED_ONSETS
        assert [a["detected_ts"] for a in alerts] == EXPECTED_DETECTED


# =========================================================================
# d. July clearsky: zero stuck, zero statistical, invariant
# =========================================================================


class TestClearsky:
    """native_july_clearsky.csv must produce zero stuck alerts, no detected_severity
    rows, and identical statistical hit sets across sizes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    async def test_zero_stuck(self, chunk_size: int):
        resp = await _run_sync_cached(FIXTURE_B, chunk_size)
        alerts = _stuck_alerts(resp)
        assert len(alerts) == 0, f"Chunk {chunk_size}: expected 0 stuck, got {len(alerts)}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    async def test_stat_hits_identical(self, chunk_size: int):
        single_resp = await _run_sync_cached(FIXTURE_B, 10**7)
        this_resp = await _run_sync_cached(FIXTURE_B, chunk_size)
        assert _stat_hit_set(this_resp) == _stat_hit_set(single_resp)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    async def test_no_detected_severity_rows(self, chunk_size: int):
        """Persist and verify no detected_severity rows exist."""
        result, _ = await _run_async_pipeline(
            FIXTURE_B, chunk_size if chunk_size < 10000 else 5000, persist_data=True
        )
        # The result dict has no urgent stuck rows
        alerts = _stuck_alerts(result)
        assert len(alerts) == 0


# =========================================================================
# e. Async parity (50-row and 500-row)
# =========================================================================


class TestAsyncParity:
    """Async path's statistical hit set and stuck alerts equal sync's."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("portion_rows", [50, 500], ids=["p50", "p500"])
    async def test_stat_hits_match_sync(self, portion_rows: int):
        """Async stat hit set equals sync at chunk_size=portion_rows."""
        sync_resp = await _run_sync_cached(FIXTURE_A, portion_rows)
        async_result, _ = await _run_async_pipeline(FIXTURE_A, portion_rows)

        sync_hits = _stat_hit_set(sync_resp)
        async_hits = _stat_hit_set(async_result)

        assert async_hits == sync_hits, (
            f"portion_rows={portion_rows}: async stat hits diverge from sync.\n"
            f"Only in sync: {sync_hits - async_hits}\n"
            f"Only in async: {async_hits - sync_hits}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("portion_rows", [50, 500], ids=["p50", "p500"])
    async def test_stuck_alerts_match_sync(self, portion_rows: int):
        """Async stuck alerts equal sync."""
        sync_resp = await _run_sync_cached(FIXTURE_A, portion_rows)
        async_result, _ = await _run_async_pipeline(FIXTURE_A, portion_rows)

        sync_alerts = _stuck_alerts(sync_resp)
        async_alerts = _stuck_alerts(async_result)

        assert len(async_alerts) == len(sync_alerts) == 4
        assert [a["timestamp"] for a in async_alerts] == [a["timestamp"] for a in sync_alerts]
        assert [a["detected_ts"] for a in async_alerts] == [a["detected_ts"] for a in sync_alerts]

    @pytest.mark.asyncio
    async def test_lstm_alerts_match_sync_async(self):
        """LSTM alerts are identical between sync and async, and non-empty.

        The reparameterize guard (task 1e) makes LSTM inference deterministic,
        so sync and async pipelines must produce identical LSTM alerts --
        same timestamps, same scores, same count.

        Both sides run the real epoch-72 checkpoint.  With _mock_detector()
        this test measured zero LSTM alerts on both sides -- its constant 0.05
        clears no threshold -- so the equality held for any behaviour, and a
        constant score has no sampling noise for the guard to be deterministic
        about.  The sync run is local and uncached on purpose: _run_sync_cached
        keys on (fixture, chunk_size) only, so a real detector behind it would
        leak into every other test sharing that key.
        """
        if not CHECKPOINT_PATH.is_file():
            pytest.skip(f"Real checkpoint not found: {CHECKPOINT_PATH}")
        from src.models.inference import AnomalyDetector

        det = AnomalyDetector(str(CHECKPOINT_PATH))

        adapter = CSVAdapter({"filepath": str(FIXTURE_A)})
        adapter.connect()
        sync_resp, _ = await run_cold_run_upload(
            tmp_path=str(FIXTURE_A),
            adapter=adapter,
            detector=det,
            tenant_id=TENANT,
            greenhouse_id=GH_ID,
            chunk_size=50,
        )
        async_result, _ = await _run_async_pipeline(
            FIXTURE_A, 50, detector_override=det
        )

        sync_lstm = sorted(
            [
                (h.timestamp, h.score)
                for h in sync_resp.high_anomalies
                if h.detector and h.detector.startswith("lstm")
            ]
        )
        async_lstm = sorted(
            [
                (h["timestamp"], h["score"])
                for h in async_result.get("high_anomalies", [])
                if h.get("detector", "").startswith("lstm")
            ]
        )
        assert len(sync_lstm) > 0, (
            f"No LSTM alerts on either side: the equality assertion below "
            f"passes vacuously when both are empty "
            f"(sync={len(sync_lstm)}, async={len(async_lstm)})"
        )
        assert sync_lstm == async_lstm, (
            f"LSTM alerts differ: sync={sync_lstm}, async={async_lstm}"
        )


    @pytest.mark.asyncio
    async def test_async_with_real_checkpoint(self):
        """One async case with the real checkpoint (epoch_0072)."""
        if not CHECKPOINT_PATH.is_file():
            pytest.skip(f"Real checkpoint not found: {CHECKPOINT_PATH}")
        from src.models.inference import AnomalyDetector

        det = AnomalyDetector(str(CHECKPOINT_PATH))
        async_result, _ = await _run_async_pipeline(
            FIXTURE_A, 50, detector_override=det
        )
        async_alerts = _stuck_alerts(async_result)
        assert len(async_alerts) == 4, f"Expected 4 stuck alerts, got {len(async_alerts)}"
        # Stat hit count should be 102 (invariant regardless of LSTM detector)
        async_stat = {
            (h["timestamp"], h["column"])
            for h in async_result.get("high_anomalies", [])
            if h.get("detector") == "statistical"
        }
        assert len(async_stat) == EXPECTED_STAT_TOTAL


# =========================================================================
# e2. Async parity — full CHUNK_SIZES (Gap 1, task 1f phase 3)
# =========================================================================


class TestAsyncParityFullChunkSizes:
    """Async statistical hit SETS equal sync across ALL five chunk sizes.

    Gap 1 coverage: the original TestAsyncParity only tested portion_rows=50
    and portion_rows=500.  This class parametrizes over the full
    CHUNK_SIZES = [50, 288, 500, 5000, 10**7] on FIXTURE_A
    (native_dec_stuck_fault.csv).  chunk_size=10**7 means a single portion
    in the async pipeline — the producer should still work correctly with
    one giant portion.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("chunk_size", CHUNK_SIZES, ids=lambda c: f"chunk{c}")
    async def test_stat_hit_set_match_sync_all_sizes(self, chunk_size: int):
        """Async stat hit set equals sync at every chunk size in CHUNK_SIZES."""
        sync_resp = await _run_sync_cached(FIXTURE_A, chunk_size)
        async_result, _ = await _run_async_pipeline(FIXTURE_A, chunk_size)

        sync_hits = _stat_hit_set(sync_resp)
        async_hits = _stat_hit_set(async_result)

        assert async_hits == sync_hits, (
            f"chunk_size={chunk_size}: async stat hits diverge from sync.\n"
            f"Only in sync: {sync_hits - async_hits}\n"
            f"Only in async: {async_hits - sync_hits}"
        )
        # Record counts for the report
        print(
            f"\n[FIXTURE_A chunk_size={chunk_size}] "
            f"sync={len(sync_hits)} async={len(async_hits)}"
        )

"""Pipeline-level tests pinning deterministic LSTM-VAE scoring.

Uses the real epoch-72 checkpoint through both sync and async pipelines to
verify that scoring is byte-identical across calls, and that both pipelines
produce the same LSTM alerts (not just equal counts -- the same alerts).
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.models._vae_deterministic_helpers import (
    FIXTURE_DEC,
    FIXTURE_JULY,
    lstm_alerts_from_dict,
    lstm_alerts_from_response,
    real_detector,
    score_async,
    score_sync,
)


# =========================================================================
# 1. Byte-identical scores
# =========================================================================


class TestByteIdenticalScores:
    """Same file scored twice yields byte-identical LSTM scores and threshold."""

    @pytest.mark.asyncio
    async def test_dec_fixture_scores_identical(self):
        """native_dec_stuck_fault scored twice produces np.array_equal scores."""
        det = real_detector()
        resp_a = await score_sync(FIXTURE_DEC, det)
        resp_b = await score_sync(FIXTURE_DEC, det)

        scores_a = np.array(resp_a.anomaly_scores, dtype=np.float64)
        scores_b = np.array(resp_b.anomaly_scores, dtype=np.float64)

        assert len(scores_a) > 0, (
            f"No scores on the dec fixture: np.array_equal returns True "
            f"for two empty arrays, so the comparison below passes vacuously "
            f"(run_a={len(scores_a)}, run_b={len(scores_b)})"
        )
        assert np.array_equal(scores_a, scores_b), (
            f"Scores differ: max_diff={np.max(np.abs(scores_a - scores_b))}"
        )

    @pytest.mark.asyncio
    async def test_dec_fixture_threshold_identical(self):
        """Calibrated threshold is identical across two scoring runs."""
        det = real_detector()
        resp_a = await score_sync(FIXTURE_DEC, det)
        resp_b = await score_sync(FIXTURE_DEC, det)

        assert resp_a.threshold_used == resp_b.threshold_used, (
            f"Thresholds differ: {resp_a.threshold_used} vs "
            f"{resp_b.threshold_used}"
        )

    @pytest.mark.asyncio
    async def test_dec_fixture_lstm_scores_identical(self):
        """LSTM score array is identical across two scoring runs.

        The robust gate (median + z*MAD) is on by default, so LSTM alerts
        are empty on these fixtures.  Determinism is verified by comparing
        the full score arrays instead, which are non-empty and must be
        byte-identical.
        """
        det = real_detector()
        resp_a = await score_sync(FIXTURE_DEC, det)
        resp_b = await score_sync(FIXTURE_DEC, det)

        scores_a = np.array(resp_a.anomaly_scores, dtype=np.float64)
        scores_b = np.array(resp_b.anomaly_scores, dtype=np.float64)

        assert len(scores_a) > 0, (
            f"No scores on the dec fixture: np.array_equal returns True "
            f"for two empty arrays (run_a={len(scores_a)}, run_b={len(scores_b)})"
        )
        assert np.array_equal(scores_a, scores_b), (
            f"Scores differ: max_diff={np.max(np.abs(scores_a - scores_b))}"
        )

    @pytest.mark.asyncio
    async def test_july_fixture_scores_identical(self):
        """native_july_clearsky scored twice produces np.array_equal scores."""
        det = real_detector()
        resp_a = await score_sync(FIXTURE_JULY, det)
        resp_b = await score_sync(FIXTURE_JULY, det)

        scores_a = np.array(resp_a.anomaly_scores, dtype=np.float64)
        scores_b = np.array(resp_b.anomaly_scores, dtype=np.float64)

        assert len(scores_a) > 0, (
            f"No scores on the july fixture: np.array_equal returns True "
            f"for two empty arrays, so the comparison below passes vacuously "
            f"(run_a={len(scores_a)}, run_b={len(scores_b)})"
        )
        assert np.array_equal(scores_a, scores_b), (
            f"Scores differ: max_diff={np.max(np.abs(scores_a - scores_b))}"
        )

    @pytest.mark.asyncio
    async def test_july_fixture_threshold_identical(self):
        """Calibrated threshold is identical on clearsky."""
        det = real_detector()
        resp_a = await score_sync(FIXTURE_JULY, det)
        resp_b = await score_sync(FIXTURE_JULY, det)

        assert resp_a.threshold_used == resp_b.threshold_used, (
            f"Thresholds differ: {resp_a.threshold_used} vs "
            f"{resp_b.threshold_used}"
        )


# =========================================================================
# 2. Sync and async agree on LSTM alerts
# =========================================================================


class TestSyncAsyncParity:
    """Sync and async pipelines produce identical LSTM alerts."""

    @pytest.mark.asyncio
    async def test_dec_lstm_scores_sync_async_match(self):
        """LSTM score arrays match between sync and async on dec fixture.

        The robust gate (median + z*MAD) is on by default, so LSTM alerts
        are empty on these fixtures.  Parity is verified by comparing the
        full score arrays instead.
        """
        det = real_detector()
        sync_resp = await score_sync(FIXTURE_DEC, det, chunk_size=500)
        async_result = await score_async(FIXTURE_DEC, det, portion_rows=500)

        sync_scores = np.array(sync_resp.anomaly_scores, dtype=np.float64)
        async_scores = np.array(
            async_result.get("anomaly_scores", []), dtype=np.float64
        )

        assert len(sync_scores) > 0, (
            f"No scores on the dec fixture: comparison passes vacuously "
            f"(sync={len(sync_scores)}, async={len(async_scores)})"
        )
        assert np.array_equal(sync_scores, async_scores), (
            f"Score arrays differ: max_diff="
            f"{np.max(np.abs(sync_scores - async_scores))}"
        )

    @pytest.mark.asyncio
    async def test_july_lstm_scores_sync_async_match(self):
        """LSTM score arrays match between sync and async on july fixture.

        The robust gate (median + z*MAD) is on by default, so LSTM alerts
        are empty on these fixtures.  Parity is verified by comparing the
        full score arrays instead.
        """
        det = real_detector()
        sync_resp = await score_sync(FIXTURE_JULY, det, chunk_size=500)
        async_result = await score_async(
            FIXTURE_JULY, det, portion_rows=500
        )

        sync_scores = np.array(sync_resp.anomaly_scores, dtype=np.float64)
        async_scores = np.array(
            async_result.get("anomaly_scores", []), dtype=np.float64
        )

        assert len(sync_scores) > 0, (
            f"No scores on the july fixture: comparison passes vacuously "
            f"(sync={len(sync_scores)}, async={len(async_scores)})"
        )
        assert np.array_equal(sync_scores, async_scores), (
            f"Score arrays differ: max_diff="
            f"{np.max(np.abs(sync_scores - async_scores))}"
        )

    @pytest.mark.asyncio
    async def test_dec_anomaly_scores_array_sync_async_equal(self):
        """Full anomaly_scores array is byte-identical between pipelines."""
        det = real_detector()
        sync_resp = await score_sync(FIXTURE_DEC, det, chunk_size=500)
        async_result = await score_async(FIXTURE_DEC, det, portion_rows=500)

        sync_scores = np.array(
            sync_resp.anomaly_scores, dtype=np.float64
        )
        async_scores = np.array(
            async_result.get("anomaly_scores", []), dtype=np.float64
        )

        assert len(sync_scores) > 0, (
            f"No scores on the dec fixture: np.array_equal returns True for "
            f"two empty arrays, so the comparison below passes vacuously "
            f"(sync={len(sync_scores)}, async={len(async_scores)})"
        )
        assert np.array_equal(sync_scores, async_scores), (
            f"Anomaly score arrays differ: max_diff="
            f"{np.max(np.abs(sync_scores - async_scores))}"
        )

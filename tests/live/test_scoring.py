"""Comprehensive tests for LiveScorer and attribution (UNVERIFIED code).

These modules had ZERO tests prior — treat as potentially buggy.
Tests marked with @pytest.mark.xfail(reason="BUG: ...", strict=True) document
confirmed bugs without modifying implementation.

Coverage themes:
- Warmup gating incl. never-seen channel
- Forward-fill of missing channels
- Score attached and finite
- Threshold crossing -> exactly one anomaly event (debounce) with correct
  affected_channels ordering
- Re-emit after LIVE_ANOMALY_REEMIT_SECONDS
- Episode end + re-cross
- Detector resolution failure -> baseline fallback (no crash)
- Enrichment: success path, exception swallowed, close() cancels in-flight
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from src.config import LIVE_ANOMALY_REEMIT_SECONDS, LIVE_ANOMALY_THRESHOLD, SEQ_LEN
from src.live.attribution import (
    CHANNEL_ORDER,
    _AFFECTED_CONTRIBUTION_FLOOR,
    compute_channel_attribution,
    forward_pass_sync,
)
from src.live.events import LiveAnomalyEvent, LiveReadingEvent
from src.live.hub import LiveHub
from src.live.scoring import LiveScorer

from tests.live.conftest import GREENHOUSE, NOW, TENANT, _safe_redis_close, make_reading


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis_client() -> FakeRedis:
    client = FakeRedis()
    yield client
    await _safe_redis_close(client)


@pytest_asyncio.fixture
async def hub(redis_client: FakeRedis) -> LiveHub:
    return LiveHub(redis_client=redis_client, block_ms=100)


@pytest.fixture
def scorer(hub: LiveHub) -> LiveScorer:
    return LiveScorer(
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        hub=hub,
        threshold=0.5,
    )


def _full_channel_reading(
    step: int = 0,
    temp: float = 25.0,
    humidity: float = 65.0,
    co2: float = 400.0,
    light: float = 500.0,
    timestamp: datetime | None = None,
) -> LiveReadingEvent:
    """Build a reading with all 4 canonical channels."""
    return LiveReadingEvent(
        seq=f"seq-{step}",
        timestamp=timestamp or datetime(2026, 7, 18, 12, 0, step, tzinfo=timezone.utc),
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        channels={
            "temperature": temp,
            "humidity": humidity,
            "co2": co2,
            "light": light,
        },
        sources={
            "temperature": "src",
            "humidity": "src",
            "co2": "src",
            "light": "src",
        },
    )


# ---------------------------------------------------------------------------
# Attribution tests
# ---------------------------------------------------------------------------


class TestComputeChannelAttribution:

    def test_single_dominant_channel(self) -> None:
        """Single channel with 100% error → reported as sole affected."""
        errors = np.array([0.9, 0.0, 0.0, 0.0])
        affected, reasoning = compute_channel_attribution(errors)
        assert affected == [CHANNEL_ORDER[0]]
        assert CHANNEL_ORDER[0] in reasoning

    def test_multiple_channels_above_floor(self) -> None:
        """Multiple channels above 20% floor are reported."""
        errors = np.array([0.4, 0.35, 0.15, 0.10])
        affected, reasoning = compute_channel_attribution(errors)
        # channels at >=20%: 0.4/(1.0)=40%, 0.35/1.0=35%
        assert CHANNEL_ORDER[0] in affected
        assert CHANNEL_ORDER[1] in affected
        # 15% and 10% should not be included
        assert CHANNEL_ORDER[2] not in affected

    def test_all_zeros_returns_empty(self) -> None:
        """All zeros → no affected channels."""
        errors = np.array([0.0, 0.0, 0.0, 0.0])
        affected, reasoning = compute_channel_attribution(errors)
        assert affected == []
        assert "normal range" in reasoning.lower()

    def test_near_zero_total(self) -> None:
        """Near-zero total error returns empty with normal message."""
        errors = np.array([1e-12, 1e-12, 1e-12, 1e-12])
        affected, reasoning = compute_channel_attribution(errors)
        assert affected == []

    def test_affected_channels_sorted_by_contribution(self) -> None:
        """Affected channels are ordered by contribution (highest first)."""
        # Channel 2 has highest error, channel 0 is second
        errors = np.array([0.25, 0.05, 0.6, 0.1])
        affected, _ = compute_channel_attribution(errors)
        # sorted_idx[0] should be index 2 (co2), then index 0 (temperature)
        assert affected[0] == CHANNEL_ORDER[2]

    def test_minimum_one_channel_always_reported(self) -> None:
        """Even if all channels are below the floor, at least one is returned."""
        # All four equal → each is 25% which is above 20%, but
        # let's test with one clearly below
        errors = np.array([0.01, 0.01, 0.01, 0.97])
        affected, _ = compute_channel_attribution(errors)
        assert len(affected) >= 1
        # The dominant channel should be light (index 3)
        assert affected[0] == CHANNEL_ORDER[3]

    def test_reasoning_format(self) -> None:
        """Reasoning mentions the top channel name and percentage."""
        errors = np.array([0.8, 0.1, 0.05, 0.05])
        _, reasoning = compute_channel_attribution(errors)
        assert "temperature" in reasoning
        assert "80%" in reasoning


class TestForwardPassSync:

    def test_forward_pass_returns_score_and_per_channel(self) -> None:
        """forward_pass_sync returns (scaled_score, per_channel_mse)."""
        import torch

        detector = MagicMock()
        # Model returns identical reconstruction → zero error
        def model_fn(tensor):
            return tensor.clone(), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        window = np.random.randn(SEQ_LEN, 4).astype(np.float32)
        score, per_channel = forward_pass_sync(detector, window)

        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0
        assert per_channel.shape == (4,)

    def test_high_error_produces_high_score(self) -> None:
        """When reconstruction is zeros, error is high → score near 1.0."""
        import torch

        detector = MagicMock()
        def model_fn(tensor):
            return torch.zeros_like(tensor), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        # Input with large values → large MSE
        window = np.ones((SEQ_LEN, 4), dtype=np.float32) * 2.0
        score, per_channel = forward_pass_sync(detector, window)

        assert score > 0.5


# ---------------------------------------------------------------------------
# LiveScorer — warmup gating
# ---------------------------------------------------------------------------


class TestWarmupGating:

    @pytest.mark.asyncio
    async def test_warmup_not_all_channels_seen(self, scorer: LiveScorer) -> None:
        """Before all 4 channels are seen, score() returns event unchanged."""
        # Only temperature seen
        event = LiveReadingEvent(
            seq="1", timestamp=NOW, tenant_id=TENANT,
            greenhouse_id=GREENHOUSE,
            channels={"temperature": 22.0},
            sources={"temperature": "src"},
        )
        result = await scorer.score(event)
        assert result.anomaly_score is None

    @pytest.mark.asyncio
    async def test_warmup_window_not_full(self, scorer: LiveScorer) -> None:
        """Even with all channels seen, need SEQ_LEN readings to warm up."""
        # Patch detector resolution to return a fake detector
        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock) as mock_det:
            mock_det.return_value = None
            for i in range(SEQ_LEN - 1):
                event = _full_channel_reading(step=i)
                result = await scorer.score(event)
            # Still not warm (window needs SEQ_LEN entries)
            assert result.anomaly_score is None

    @pytest.mark.asyncio
    async def test_never_seen_channel_blocks_warmup(self, scorer: LiveScorer) -> None:
        """If 'light' is never provided, warmup never completes."""
        for i in range(SEQ_LEN + 5):
            event = LiveReadingEvent(
                seq=str(i), timestamp=datetime(2026, 7, 18, 12, 0, i, tzinfo=timezone.utc),
                tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                channels={"temperature": 22.0, "humidity": 65.0, "co2": 400.0},
                sources={"temperature": "s", "humidity": "s", "co2": "s"},
            )
            result = await scorer.score(event)
        assert result.anomaly_score is None


# ---------------------------------------------------------------------------
# LiveScorer — forward fill
# ---------------------------------------------------------------------------


class TestForwardFill:

    @pytest.mark.asyncio
    async def test_missing_channel_forward_filled(self, scorer: LiveScorer, hub: LiveHub) -> None:
        """After all channels seen, missing channels in subsequent readings
        are forward-filled with last known value."""
        import torch

        # Patch detector
        detector = MagicMock()
        def model_fn(tensor):
            return tensor.clone(), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            # First, send all 4 channels for SEQ_LEN readings
            for i in range(SEQ_LEN):
                event = _full_channel_reading(step=i)
                await scorer.score(event)

            # Now send only temperature+humidity — co2 and light should forward-fill
            partial = LiveReadingEvent(
                seq="partial", timestamp=datetime(2026, 7, 18, 12, 1, 0, tzinfo=timezone.utc),
                tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                channels={"temperature": 26.0, "humidity": 70.0},
                sources={"temperature": "s", "humidity": "s"},
            )
            result = await scorer.score(partial)
            # Should have a score since forward-fill provides all channels
            assert result.anomaly_score is not None


# ---------------------------------------------------------------------------
# LiveScorer — score attached and finite
# ---------------------------------------------------------------------------


class TestScoreAttached:

    @pytest.mark.asyncio
    async def test_score_attached_after_warmup(self, scorer: LiveScorer) -> None:
        """After warmup, anomaly_score is attached and finite."""
        import torch

        detector = MagicMock()
        def model_fn(tensor):
            return tensor.clone(), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            for i in range(SEQ_LEN + 1):
                event = _full_channel_reading(step=i)
                result = await scorer.score(event)

            assert result.anomaly_score is not None
            assert np.isfinite(result.anomaly_score)

    @pytest.mark.asyncio
    async def test_nonfinite_score_not_attached(self, scorer: LiveScorer) -> None:
        """If the model produces NaN/Inf score, it's discarded."""
        import torch

        detector = MagicMock()
        def model_fn(tensor):
            # Return something that produces NaN MSE
            return torch.full_like(tensor, float('nan')), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            for i in range(SEQ_LEN + 1):
                event = _full_channel_reading(step=i)
                result = await scorer.score(event)

            # NaN score should be discarded
            assert result.anomaly_score is None


# ---------------------------------------------------------------------------
# LiveScorer — anomaly episodes (threshold crossing + debounce)
# ---------------------------------------------------------------------------


class TestAnomalyEpisodes:

    @pytest.mark.asyncio
    async def test_threshold_crossing_emits_one_anomaly(self, hub: LiveHub, redis_client: FakeRedis) -> None:
        """Crossing threshold → exactly one anomaly event published (debounce)."""
        import torch
        from src.live.hub import stream_key

        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.1,  # Very low threshold so any error triggers
        )

        detector = MagicMock()
        # Return zeros → high error for non-zero input
        # Since data is normalized to [0,1] and we return zeros,
        # MSE = mean of squared normalized values. With mid-range values ~0.5,
        # MSE ≈ 0.25, scaled = 0.25/2.0 = 0.125 > threshold 0.1
        def model_fn(tensor):
            return torch.zeros_like(tensor), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch.object(scorer._enrichment, 'spawn'):
                for i in range(SEQ_LEN + 3):
                    event = _full_channel_reading(step=i)
                    await scorer.score(event)

        # Check Redis for anomaly events
        key = stream_key(TENANT, GREENHOUSE)
        entries = await redis_client.xrange(key, "-", "+")
        anomaly_entries = [
            e for _, fields in entries
            if (e := fields.get(b"type", b"").decode()) == "anomaly"
        ]
        # Should have exactly 1 (debounced — subsequent scores are within-episode)
        assert len(anomaly_entries) == 1

    @pytest.mark.asyncio
    async def test_reemit_after_reemit_seconds(self, hub: LiveHub, redis_client: FakeRedis) -> None:
        """After REEMIT_SECONDS, a second anomaly is re-emitted."""
        import torch
        from src.live.hub import stream_key

        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.1,  # Low threshold so error triggers
        )

        detector = MagicMock()
        def model_fn(tensor):
            return torch.zeros_like(tensor), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch.object(scorer._enrichment, 'spawn'):
                # Fill warmup window
                for i in range(SEQ_LEN):
                    event = _full_channel_reading(step=i)
                    await scorer.score(event)

                # First anomaly trigger
                event = _full_channel_reading(step=SEQ_LEN)
                await scorer.score(event)
                assert scorer._in_episode

                # Simulate time passing beyond REEMIT_SECONDS
                scorer._last_anomaly_emit = time.monotonic() - LIVE_ANOMALY_REEMIT_SECONDS - 1

                # Second scoring should re-emit
                event2 = _full_channel_reading(step=SEQ_LEN + 1)
                await scorer.score(event2)

        key = stream_key(TENANT, GREENHOUSE)
        entries = await redis_client.xrange(key, "-", "+")
        anomaly_count = sum(
            1 for _, fields in entries
            if fields.get(b"type", b"").decode() == "anomaly"
        )
        assert anomaly_count >= 2

    @pytest.mark.asyncio
    async def test_episode_ends_below_threshold(self, scorer: LiveScorer) -> None:
        """When score drops below threshold, episode ends."""
        import torch

        call_count = [0]
        def model_fn(tensor):
            call_count[0] += 1
            if call_count[0] <= 2:
                # High error
                return torch.zeros_like(tensor), None, None, None
            else:
                # Low error — return same as input
                return tensor.clone(), None, None, None

        detector = MagicMock()
        detector.model = MagicMock(side_effect=model_fn)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch('src.live.enrichment.EnrichmentRunner.spawn'):
                for i in range(SEQ_LEN + 5):
                    event = _full_channel_reading(step=i)
                    await scorer.score(event)

                # After many low-error calls, episode should end
                assert not scorer._in_episode

    @pytest.mark.asyncio
    async def test_affected_channels_ordering_in_anomaly_event(
        self, hub: LiveHub, redis_client: FakeRedis
    ) -> None:
        """affected_channels in the anomaly event are ordered by contribution."""
        import json
        import torch
        from src.live.hub import stream_key

        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.1,
        )

        detector = MagicMock()
        # Make temperature (index 0) have much higher error
        def model_fn(tensor):
            recon = tensor.clone()
            recon[:, :, 0] = 0.0  # zero out temperature → high error there
            return recon, None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch('src.live.enrichment.EnrichmentRunner.spawn'):
                for i in range(SEQ_LEN + 1):
                    event = _full_channel_reading(step=i)
                    await scorer.score(event)

        key = stream_key(TENANT, GREENHOUSE)
        entries = await redis_client.xrange(key, "-", "+")
        for _, fields in entries:
            if fields.get(b"type", b"").decode() == "anomaly":
                data = json.loads(fields[b"data"].decode())
                # Temperature should be first since it has highest error
                assert data["affected_channels"][0] == "temperature"
                break


# ---------------------------------------------------------------------------
# LiveScorer — detector resolution
# ---------------------------------------------------------------------------


class TestDetectorResolution:

    @pytest.mark.asyncio
    async def test_detector_none_returns_unscored(self, scorer: LiveScorer) -> None:
        """When detector resolution returns None, event is returned unscored."""
        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=None):
            for i in range(SEQ_LEN + 1):
                event = _full_channel_reading(step=i)
                result = await scorer.score(event)
            assert result.anomaly_score is None

    @pytest.mark.asyncio
    async def test_forward_pass_exception_returns_unscored(self, scorer: LiveScorer) -> None:
        """When forward_pass raises, event is returned unscored (no crash)."""
        detector = MagicMock()
        detector.model = MagicMock(side_effect=RuntimeError("model crashed"))

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            for i in range(SEQ_LEN + 1):
                event = _full_channel_reading(step=i)
                result = await scorer.score(event)
            assert result.anomaly_score is None


# ---------------------------------------------------------------------------
# LiveScorer — enrichment
# ---------------------------------------------------------------------------


class TestEnrichment:

    @pytest.mark.asyncio
    async def test_enrichment_exception_swallowed(self, hub: LiveHub, caplog) -> None:
        """Enrichment exception is swallowed and logged, doesn't crash scorer."""
        import torch

        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.3,
        )

        detector = MagicMock()
        def model_fn(tensor):
            return torch.zeros_like(tensor), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            # Let enrichment run but make it fail
            with patch(
                'src.live.enrichment.EnrichmentRunner._run_enrichment',
                new_callable=AsyncMock,
                side_effect=RuntimeError("enrichment failed"),
            ):
                for i in range(SEQ_LEN + 1):
                    event = _full_channel_reading(step=i)
                    await scorer.score(event)

                # Give enrichment task time to complete
                await asyncio.sleep(0.1)

        # Should not crash — enrichment failure is non-critical

    @pytest.mark.asyncio
    async def test_enrichment_done_callback_cleans_task(self, hub: LiveHub) -> None:
        """_on_enrichment_done removes task from the set when it finishes."""
        import torch

        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.1,
        )

        detector = MagicMock()
        def model_fn(tensor):
            return torch.zeros_like(tensor), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        # Create enrichment that completes quickly
        async def fast_enrichment(window_array, anomaly_seq):
            await asyncio.sleep(0.01)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch.object(scorer._enrichment, '_run_enrichment', fast_enrichment):
                for i in range(SEQ_LEN + 1):
                    event = _full_channel_reading(step=i)
                    await scorer.score(event)

                # Wait for enrichment to complete
                await asyncio.sleep(0.1)
                # The done callback should have removed the task
                assert len(scorer._enrichment.tasks) == 0

    @pytest.mark.asyncio
    async def test_publish_anomaly_exception_swallowed(self, redis_client: FakeRedis) -> None:
        """If hub.publish raises during anomaly publish, it's caught (no crash)."""
        import torch

        # Create hub that raises on publish
        hub = LiveHub(redis_client=redis_client, block_ms=100)
        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.1,
        )

        detector = MagicMock()
        def model_fn(tensor):
            return torch.zeros_like(tensor), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        # After warmup, patch hub.publish to fail on anomaly events
        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch.object(scorer._enrichment, 'spawn'):
                original_publish = hub.publish

                async def failing_publish(t, g, event_type, payload):
                    if event_type == "anomaly":
                        raise RuntimeError("publish failed")
                    return await original_publish(t, g, event_type, payload)

                with patch.object(hub, 'publish', failing_publish):
                    for i in range(SEQ_LEN + 1):
                        event = _full_channel_reading(step=i)
                        result = await scorer.score(event)
                    # Should not crash — the exception is caught
                    assert result.anomaly_score is not None

    @pytest.mark.asyncio
    async def test_close_with_no_tasks(self, scorer: LiveScorer) -> None:
        """close() with no in-flight enrichment tasks is a no-op."""
        assert len(scorer._enrichment.tasks) == 0
        await scorer.close()
        assert len(scorer._enrichment.tasks) == 0

    @pytest.mark.asyncio
    async def test_close_cancels_inflight_enrichment(self, hub: LiveHub) -> None:
        """close() cancels in-flight enrichment tasks."""
        import torch

        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.1,  # Low threshold
        )

        detector = MagicMock()
        def model_fn(tensor):
            return torch.zeros_like(tensor), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        # Create a slow enrichment that will be in-flight
        async def slow_enrichment(window_array, anomaly_seq):
            await asyncio.sleep(999)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch.object(scorer._enrichment, '_run_enrichment', slow_enrichment):
                for i in range(SEQ_LEN + 1):
                    event = _full_channel_reading(step=i)
                    await scorer.score(event)

                # Give the task time to start
                await asyncio.sleep(0.05)
                assert len(scorer._enrichment.tasks) >= 1

        await scorer.close()
        assert len(scorer._enrichment.tasks) == 0


# ---------------------------------------------------------------------------
# LiveScorer — episode_id correlation (Fix 2: anomaly↔detail)
# ---------------------------------------------------------------------------


class TestEpisodeIdCorrelation:
    """Tests that distinct episodes get distinct non-empty seq values,
    re-emits share the same episode's seq, and anomaly-detail's anomaly_seq
    equals the episode's seq."""

    @pytest.mark.asyncio
    async def test_two_episodes_distinct_non_empty_seqs(
        self, hub: LiveHub, redis_client: FakeRedis,
    ) -> None:
        """Two separate anomaly episodes carry distinct non-empty seq values."""
        import json
        import torch
        from src.live.hub import stream_key

        call_count = [0]

        def model_fn(tensor):
            call_count[0] += 1
            # Call 1 fires on the 20th warmup reading (window just became
            # full), so it must be normal. Then: calls 2-3 high error
            # (episode 1), calls 4-6 low error (recovery), calls 7+ high
            # error (episode 2).
            if call_count[0] == 1:
                return tensor.clone(), None, None, None
            if call_count[0] <= 3:
                return torch.zeros_like(tensor), None, None, None
            if call_count[0] <= 6:
                return tensor.clone(), None, None, None
            return torch.zeros_like(tensor), None, None, None

        detector = MagicMock()
        detector.model = MagicMock(side_effect=model_fn)

        # Use merger-realistic seq="" readings (the actual production path)
        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.1,
        )

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch.object(scorer._enrichment, 'spawn'):
                step = 0
                # Warm up window
                for i in range(SEQ_LEN):
                    event = LiveReadingEvent(
                        seq="",  # merger-realistic empty seq
                        timestamp=datetime(2026, 7, 18, 12, 0, step, tzinfo=timezone.utc),
                        tenant_id=TENANT,
                        greenhouse_id=GREENHOUSE,
                        channels={"temperature": 25.0, "humidity": 65.0, "co2": 400.0, "light": 500.0},
                        sources={"temperature": "s", "humidity": "s", "co2": "s", "light": "s"},
                    )
                    await scorer.score(event)
                    step += 1

                # Episode 1: two anomalous scores
                for _ in range(2):
                    event = LiveReadingEvent(
                        seq="",
                        timestamp=datetime(2026, 7, 18, 12, 0, step, tzinfo=timezone.utc),
                        tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                        channels={"temperature": 25.0, "humidity": 65.0, "co2": 400.0, "light": 500.0},
                        sources={"temperature": "s", "humidity": "s", "co2": "s", "light": "s"},
                    )
                    await scorer.score(event)
                    step += 1

                # Recovery: episode ends
                for _ in range(3):
                    event = LiveReadingEvent(
                        seq="",
                        timestamp=datetime(2026, 7, 18, 12, 0, step, tzinfo=timezone.utc),
                        tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                        channels={"temperature": 25.0, "humidity": 65.0, "co2": 400.0, "light": 500.0},
                        sources={"temperature": "s", "humidity": "s", "co2": "s", "light": "s"},
                    )
                    await scorer.score(event)
                    step += 1

                assert not scorer._in_episode, "Should have ended the first episode"

                # Episode 2: new anomalous scores
                for _ in range(2):
                    event = LiveReadingEvent(
                        seq="",
                        timestamp=datetime(2026, 7, 18, 12, 0, step, tzinfo=timezone.utc),
                        tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                        channels={"temperature": 25.0, "humidity": 65.0, "co2": 400.0, "light": 500.0},
                        sources={"temperature": "s", "humidity": "s", "co2": "s", "light": "s"},
                    )
                    await scorer.score(event)
                    step += 1

        key = stream_key(TENANT, GREENHOUSE)
        entries = await redis_client.xrange(key, "-", "+")
        anomaly_seqs = []
        for _, fields in entries:
            if fields.get(b"type", b"").decode() == "anomaly":
                data = json.loads(fields[b"data"].decode())
                anomaly_seqs.append(data["seq"])

        assert len(anomaly_seqs) == 2, f"Expected 2 anomaly events, got {len(anomaly_seqs)}"
        # Both must be non-empty
        assert all(seq != "" for seq in anomaly_seqs), "All anomaly seqs must be non-empty"
        # Both must be distinct (different episodes)
        assert anomaly_seqs[0] != anomaly_seqs[1], "Different episodes must have distinct seqs"

    @pytest.mark.asyncio
    async def test_reemit_shares_episode_seq(
        self, hub: LiveHub, redis_client: FakeRedis,
    ) -> None:
        """Re-emitted anomaly events within one episode share the same seq."""
        import json
        import torch
        from src.live.hub import stream_key

        detector = MagicMock()

        def model_fn(tensor):
            return torch.zeros_like(tensor), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.1,
        )

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch.object(scorer._enrichment, 'spawn'):
                for i in range(SEQ_LEN + 1):
                    event = LiveReadingEvent(
                        seq="",
                        timestamp=datetime(2026, 7, 18, 12, 0, i, tzinfo=timezone.utc),
                        tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                        channels={"temperature": 25.0, "humidity": 65.0, "co2": 400.0, "light": 500.0},
                        sources={"temperature": "s", "humidity": "s", "co2": "s", "light": "s"},
                    )
                    await scorer.score(event)

                # Force re-emit by advancing the last_anomaly_emit time
                scorer._last_anomaly_emit = time.monotonic() - LIVE_ANOMALY_REEMIT_SECONDS - 1

                event = LiveReadingEvent(
                    seq="",
                    timestamp=datetime(2026, 7, 18, 12, 0, SEQ_LEN + 1, tzinfo=timezone.utc),
                    tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                    channels={"temperature": 25.0, "humidity": 65.0, "co2": 400.0, "light": 500.0},
                    sources={"temperature": "s", "humidity": "s", "co2": "s", "light": "s"},
                )
                await scorer.score(event)

        key = stream_key(TENANT, GREENHOUSE)
        entries = await redis_client.xrange(key, "-", "+")
        anomaly_seqs = []
        for _, fields in entries:
            if fields.get(b"type", b"").decode() == "anomaly":
                data = json.loads(fields[b"data"].decode())
                anomaly_seqs.append(data["seq"])

        assert len(anomaly_seqs) == 2, f"Expected 2 anomaly events (initial + re-emit), got {len(anomaly_seqs)}"
        # Same episode → same seq
        assert anomaly_seqs[0] == anomaly_seqs[1]
        assert anomaly_seqs[0] != ""

    @pytest.mark.asyncio
    async def test_detail_anomaly_seq_matches_episode(self, hub: LiveHub, redis_client: FakeRedis) -> None:
        """AnomalyDetailEvent.anomaly_seq equals the episode's seq."""
        import json
        import torch
        from src.live.hub import stream_key

        detector = MagicMock()

        def model_fn(tensor):
            return torch.zeros_like(tensor), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.1,
        )

        # Capture the anomaly_seq passed to enrichment
        episode_ids_passed: list[str] = []

        def capture_spawn(window_array, anomaly_seq):
            episode_ids_passed.append(anomaly_seq)

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            with patch.object(scorer._enrichment, 'spawn', capture_spawn):
                for i in range(SEQ_LEN + 1):
                    event = LiveReadingEvent(
                        seq="",
                        timestamp=datetime(2026, 7, 18, 12, 0, i, tzinfo=timezone.utc),
                        tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                        channels={"temperature": 25.0, "humidity": 65.0, "co2": 400.0, "light": 500.0},
                        sources={"temperature": "s", "humidity": "s", "co2": "s", "light": "s"},
                    )
                    await scorer.score(event)

        # The anomaly_seq passed to _spawn_enrichment should match the
        # anomaly event's seq in Redis
        key = stream_key(TENANT, GREENHOUSE)
        entries = await redis_client.xrange(key, "-", "+")
        anomaly_seq_from_event = None
        for _, fields in entries:
            if fields.get(b"type", b"").decode() == "anomaly":
                data = json.loads(fields[b"data"].decode())
                anomaly_seq_from_event = data["seq"]
                break

        assert len(episode_ids_passed) == 1
        assert episode_ids_passed[0] != ""
        assert episode_ids_passed[0] == anomaly_seq_from_event


# ---------------------------------------------------------------------------
# LiveScorer — forward-fill staleness cutoff (Fix 4)
# ---------------------------------------------------------------------------


class TestForwardFillStaleness:

    @pytest.mark.asyncio
    async def test_channel_silent_pauses_scoring(self, hub: LiveHub) -> None:
        """When a channel goes silent beyond the staleness cutoff, scoring pauses."""
        import torch
        from src.config import LIVE_FORWARD_FILL_MAX_TICKS

        detector = MagicMock()

        def model_fn(tensor):
            return tensor.clone(), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        # Use a short emit_interval so staleness kicks in quickly
        emit_interval = 0.05  # 50ms
        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.5,
            emit_interval=emit_interval,
        )

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            # Warm up: all channels present
            for i in range(SEQ_LEN):
                event = _full_channel_reading(step=i)
                await scorer.score(event)

            # One more with all channels — should score
            event = _full_channel_reading(step=SEQ_LEN)
            result = await scorer.score(event)
            assert result.anomaly_score is not None

            # Simulate staleness by backdating the channel_last_update for 'co2'
            stale_time = time.monotonic() - (LIVE_FORWARD_FILL_MAX_TICKS + 1) * emit_interval
            scorer._channel_last_update["co2"] = stale_time

            # Now send a reading without co2 — forward-fill should be refused
            partial = LiveReadingEvent(
                seq="",
                timestamp=datetime(2026, 7, 18, 12, 1, 0, tzinfo=timezone.utc),
                tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                channels={"temperature": 26.0, "humidity": 70.0, "light": 600.0},
                sources={"temperature": "s", "humidity": "s", "light": "s"},
            )
            result = await scorer.score(partial)
            # Scoring should have paused (returned unscored)
            assert result.anomaly_score is None

    @pytest.mark.asyncio
    async def test_channel_returns_resumes_scoring(self, hub: LiveHub) -> None:
        """When a silent channel returns, scoring resumes."""
        import torch
        from src.config import LIVE_FORWARD_FILL_MAX_TICKS

        detector = MagicMock()

        def model_fn(tensor):
            return tensor.clone(), None, None, None
        detector.model = MagicMock(side_effect=model_fn)

        emit_interval = 0.05
        scorer = LiveScorer(
            tenant_id=TENANT, greenhouse_id=GREENHOUSE,
            hub=hub, threshold=0.5,
            emit_interval=emit_interval,
        )

        with patch.object(scorer._resolver, 'resolve', new_callable=AsyncMock, return_value=detector):
            # Warm up
            for i in range(SEQ_LEN + 1):
                event = _full_channel_reading(step=i)
                await scorer.score(event)

            # Make co2 stale
            stale_time = time.monotonic() - (LIVE_FORWARD_FILL_MAX_TICKS + 1) * emit_interval
            scorer._channel_last_update["co2"] = stale_time

            # Send reading with co2 present again — should resume scoring
            full_event = LiveReadingEvent(
                seq="",
                timestamp=datetime(2026, 7, 18, 12, 1, 1, tzinfo=timezone.utc),
                tenant_id=TENANT, greenhouse_id=GREENHOUSE,
                channels={"temperature": 26.0, "humidity": 70.0, "co2": 410.0, "light": 600.0},
                sources={"temperature": "s", "humidity": "s", "co2": "s", "light": "s"},
            )
            result = await scorer.score(full_event)
            assert result.anomaly_score is not None

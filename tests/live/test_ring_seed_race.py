"""Tests for the ring-seed race condition guard in LiveScorer.

Covers four scenarios:
1. Anomaly during seeding — seeding completes within the bound → enrichment
   receives the seeded windows.
2. Seeding hangs beyond the bound → enrichment proceeds after the timeout,
   warning logged.
3. Seeding raises → notify_seed_done() fires in finally → enrichment proceeds,
   no exception escapes from score().
4. Normal (non-anomalous) readings are not delayed by the seed wait at all.

Tests call _handle_episode directly (with _publish_anomaly mocked) to isolate
the race condition from the full forward-pass pipeline.  No real DB is needed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.config import SEQ_LEN
from src.live.events import LiveReadingEvent
from src.live.hub import LiveHub
from src.live.scoring import LiveScorer

from tests.live.conftest import GREENHOUSE, TENANT


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_EPOCH = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
_PER_CHANNEL_HIGH = np.array([0.9, 0.0, 0.0, 0.0], dtype=np.float32)
_PER_CHANNEL_LOW = np.zeros(4, dtype=np.float32)
_WINDOW_ARRAY = np.full((SEQ_LEN, 4), [25.0, 65.0, 400.0, 500.0], dtype=np.float32)
_FAKE_EVENT = LiveReadingEvent(
    seq="ev",
    timestamp=_EPOCH,
    tenant_id=TENANT,
    greenhouse_id=GREENHOUSE,
    channels={"temperature": 25.0, "humidity": 65.0, "co2": 400.0, "light": 500.0},
    sources={"temperature": "s", "humidity": "s", "co2": "s", "light": "s"},
)


def _make_scorer(threshold: float = 0.1, seed_started: bool = False):
    """Build a LiveScorer with mocked hub + enrichment.

    _window_timestamps is pre-populated so _publish_anomaly can read
    window_start / window_end without an IndexError.
    """
    hub_mock = MagicMock(spec=LiveHub)
    hub_mock.publish = AsyncMock()
    enrichment_mock = MagicMock()
    enrichment_mock.spawn = MagicMock()
    enrichment_mock.close = AsyncMock(return_value=None)
    scorer = LiveScorer(
        tenant_id=TENANT,
        greenhouse_id=GREENHOUSE,
        hub=hub_mock,
        threshold=threshold,
        enrichment=enrichment_mock,
    )
    # Pre-populate the timestamp deque so _publish_anomaly can read [0] and [-1].
    scorer._window_timestamps = deque([_EPOCH] * SEQ_LEN, maxlen=SEQ_LEN)
    if seed_started:
        scorer.mark_seeding_started()
    return scorer, enrichment_mock


# ---------------------------------------------------------------------------
# Test 1 — anomaly during seeding, seeding completes within the bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anomaly_waits_for_seed_completion(monkeypatch) -> None:
    """Anomaly arriving while seeding is in progress gets the seeded windows
    once seeding completes within _SEED_WAIT_TIMEOUT_SECONDS."""
    monkeypatch.setattr("src.live.scoring._SEED_WAIT_TIMEOUT_SECONDS", 0.5)

    scorer, enrichment_mock = _make_scorer(threshold=0.1, seed_started=True)

    # Constant-value window: zero variance → noise check passes → admitted to ring.
    seeded_window = np.full((SEQ_LEN, 4), [25.0, 65.0, 400.0, 500.0], dtype=np.float32)

    async def delayed_seed() -> None:
        """Complete seeding a little after the episode wait starts."""
        await asyncio.sleep(0.05)
        scorer.seed_clean_windows([seeded_window])
        scorer.notify_seed_done()

    seed_task = asyncio.create_task(delayed_seed())

    with patch.object(scorer, "_publish_anomaly", new_callable=AsyncMock):
        await scorer._handle_episode(0.9, _PER_CHANNEL_HIGH, _FAKE_EVENT, _WINDOW_ARRAY)

    await seed_task

    # Enrichment must have received the seeded window in the snapshot.
    assert enrichment_mock.spawn.called, "Expected enrichment.spawn to be called"
    noise_windows = enrichment_mock.spawn.call_args.kwargs.get("noise_clean_windows")
    assert noise_windows is not None, (
        "noise_clean_windows should contain the seeded window, not None"
    )
    assert len(noise_windows) >= 1, "Expected at least one seeded window in snapshot"


# ---------------------------------------------------------------------------
# Test 2 — seeding hangs beyond the bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_timeout_proceeds_with_warning(monkeypatch, caplog) -> None:
    """Seeding that hangs beyond _SEED_WAIT_TIMEOUT_SECONDS: enrichment proceeds
    with the empty ring after the bound, and a warning is logged."""
    monkeypatch.setattr("src.live.scoring._SEED_WAIT_TIMEOUT_SECONDS", 0.1)

    scorer, enrichment_mock = _make_scorer(threshold=0.1, seed_started=True)
    # Deliberately do NOT call notify_seed_done() — simulates a hung seed task.

    with caplog.at_level(logging.WARNING, logger="src.live.scoring"):
        with patch.object(scorer, "_publish_anomaly", new_callable=AsyncMock):
            await scorer._handle_episode(
                0.9, _PER_CHANNEL_HIGH, _FAKE_EVENT, _WINDOW_ARRAY
            )

    # Enrichment must still be spawned (scoring was not blocked forever).
    assert enrichment_mock.spawn.called, (
        "Enrichment must proceed even when seeding times out"
    )

    # Ring was empty at timeout; enrichment should receive None.
    noise_windows = enrichment_mock.spawn.call_args.kwargs.get("noise_clean_windows")
    assert noise_windows is None, (
        "Empty ring after timeout should yield noise_clean_windows=None"
    )

    # A timeout warning must have been logged.
    timeout_records = [
        r for r in caplog.records
        if "timed out" in r.message.lower() or "timeout" in r.message.lower()
    ]
    assert timeout_records, "Expected a warning to be logged on seed timeout"


# ---------------------------------------------------------------------------
# Test 3 — seeding raises, no exception escapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_exception_does_not_escape() -> None:
    """Seeding that raises must call notify_seed_done() (via finally) so enrichment
    proceeds and no exception escapes from _handle_episode."""

    scorer, enrichment_mock = _make_scorer(threshold=0.1, seed_started=True)

    async def failing_seed() -> None:
        """Mimic seed_scorer's behaviour: catch exception, signal done in finally."""
        try:
            raise RuntimeError("simulated DB failure")
        except Exception:
            pass
        finally:
            scorer.notify_seed_done()

    seed_task = asyncio.create_task(failing_seed())

    with patch.object(scorer, "_publish_anomaly", new_callable=AsyncMock):
        # Must not raise even when the seed task fails.
        await scorer._handle_episode(
            0.9, _PER_CHANNEL_HIGH, _FAKE_EVENT, _WINDOW_ARRAY
        )

    await seed_task

    # Enrichment proceeds; ring was empty, so noise_clean_windows is None.
    assert enrichment_mock.spawn.called, "Enrichment must proceed after seed failure"
    noise_windows = enrichment_mock.spawn.call_args.kwargs.get("noise_clean_windows")
    assert noise_windows is None, "Ring was empty after seed failure; expected None"


# ---------------------------------------------------------------------------
# Test 4 — normal readings are not delayed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_readings_not_delayed(monkeypatch) -> None:
    """Non-anomalous readings must not wait on _seed_ready at all.

    With _SEED_WAIT_TIMEOUT_SECONDS = 0.1 and SEQ_LEN calls, entering the
    wait on each call would take SEQ_LEN × 0.1 s = 2 s.  Wrapping the loop
    in a 1-second asyncio.wait_for proves normal scoring skips the wait
    even when _seed_ready is unset.
    """
    monkeypatch.setattr("src.live.scoring._SEED_WAIT_TIMEOUT_SECONDS", 0.1)

    # High threshold (0.9) so the score (0.01) never triggers an episode.
    scorer, enrichment_mock = _make_scorer(threshold=0.9, seed_started=True)
    # _seed_ready is NOT set; entering the wait code would impose delays.

    async def run_normal_readings() -> None:
        for _ in range(SEQ_LEN):
            await scorer._handle_episode(
                0.01, _PER_CHANNEL_LOW, _FAKE_EVENT, _WINDOW_ARRAY
            )

    # Must finish well under SEQ_LEN * _SEED_WAIT_TIMEOUT_SECONDS = 2 s.
    await asyncio.wait_for(run_normal_readings(), timeout=1.0)

    # Confirm no anomaly episode was started.
    enrichment_mock.spawn.assert_not_called()

    # Event is still not set — the normal path never touched it.
    assert not scorer._seed_ready.is_set()

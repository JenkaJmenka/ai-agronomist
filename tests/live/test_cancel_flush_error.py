"""Regression tests for cancellation-safe cleanup flush.

Guards the fix at:
  src/live/merge.py       — ChannelMerger.run() CancelledError handler
  src/live/persistence.py — PersistenceBatcher.run() CancelledError handler

THE BUG (before the fix):
  When the cleanup flush raised an exception (Redis unavailable, DB blip, …),
  that exception REPLACED the in-flight CancelledError.  supervised() then
  misclassified the task as "crashed" and restarted it with backoff instead of
  propagating the cancellation.  The restarted task kept running, so
  ``asyncio.gather(*self._tasks, return_exceptions=True)`` in LiveRuntime.stop()
  never returned — causing a hang that could keep a container alive for hours.

THE FIX:
  The cleanup flush is still attempted (buffered sensor data should be preserved
  on a clean shutdown).  If the flush raises, the exception is caught, logged
  with context, and the CancelledError is always re-raised.

TESTS:
  Each test pair covers one site (merge / persistence):
    1. The task terminates and asyncio.gather() completes — the hang is gone.
    2. The flush is still attempted despite the cancellation.
    3. The flush failure is logged (not silently swallowed).

BOUND:
  Every test that exercises the cancellation path is wrapped in asyncio.wait_for
  with _CANCEL_TIMEOUT_SECONDS.  A regression causes a TimeoutError (fast fail)
  instead of an infinite hang that locks CI.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.live.merge import ChannelMerger
from src.live.persistence import PersistenceBatcher
from src.live.sources.base import SourceConfig
from src.live.supervision import supervised

# --------------------------------------------------------------------------
# Bound: all tests that exercise cancellation must complete within this limit.
# A regression turns a hang into a TimeoutError — fast fail, not indefinite CI lock.
# --------------------------------------------------------------------------
_CANCEL_TIMEOUT_SECONDS: float = 5.0

_TENANT = "cancel_test_tenant"
_GH = "cancel_test_gh"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_source_config() -> SourceConfig:
    return SourceConfig(
        source_id="test_source",
        vendor="mock",
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        channels=["temperature"],
        priority=10,
        # Long interval so the run-loop sleep never fires during a test.
        poll_interval_seconds=3600.0,
    )


def _make_merger() -> ChannelMerger:
    """A ChannelMerger with no-op callbacks and a long emit interval."""

    async def _noop_reading(event: object) -> None:
        pass

    async def _noop_status(event: object) -> None:
        pass

    return ChannelMerger(
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        # Long so the run-loop sleep never auto-fires during the test.
        emit_interval=3600.0,
        source_configs=[_make_source_config()],
        on_reading=_noop_reading,
        on_status=_noop_status,
        grace_seconds=0.0,
    )


def _make_batcher() -> PersistenceBatcher:
    """A PersistenceBatcher with a stub session factory and very long flush interval."""
    # Pass a non-None session_factory to avoid importing the real AsyncSessionLocal
    # at __init__ time (which requires a live database).  The factory is never
    # called in these tests because _flush is replaced before the cancel fires.
    stub_factory = MagicMock()
    return PersistenceBatcher(
        flush_interval=3600.0,
        session_factory=stub_factory,
    )


# --------------------------------------------------------------------------
# ChannelMerger — cancel with failing flush
# --------------------------------------------------------------------------


class TestMergerCancelFlushError:
    """ChannelMerger.run(): CancelledError must propagate even when flush raises."""

    async def test_gather_completes_when_flush_raises(self) -> None:
        """asyncio.gather() must complete within the timeout even if _flush_all_buckets raises.

        This is the primary regression test for the hang bug.  Before the fix,
        supervised() restarted the merger (because CancelledError was replaced by
        RuntimeError from the flush), so gather() never returned.
        """
        merger = _make_merger()

        async def _raising_flush() -> None:
            raise RuntimeError("Redis unavailable — simulated flush failure")

        merger._flush_all_buckets = _raising_flush  # type: ignore[method-assign]

        # Wrap in supervised() exactly as LiveRuntime.start() does.
        task = asyncio.create_task(supervised("test_merger", merger.run))
        # Yield once so the task enters its asyncio.sleep() before we cancel.
        await asyncio.sleep(0)
        task.cancel()

        # Must complete within the timeout.  Before the fix this hung until the
        # supervised() backoff + re-entry loop kept the task alive indefinitely.
        results = await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True),
            timeout=_CANCEL_TIMEOUT_SECONDS,
        )

        assert len(results) == 1
        assert isinstance(results[0], asyncio.CancelledError), (
            f"Expected CancelledError from gather; got {results[0]!r}"
        )

    async def test_flush_is_attempted_on_cancel(self) -> None:
        """_flush_all_buckets must be called during cancel even if the flush then raises.

        The whole point of the flush is to preserve buffered sensor data on
        clean shutdown — skipping it entirely would defeat that purpose.
        """
        merger = _make_merger()
        flush_called = False

        async def _tracking_flush() -> None:
            nonlocal flush_called
            flush_called = True
            raise RuntimeError("simulated failure after attempting flush")

        merger._flush_all_buckets = _tracking_flush  # type: ignore[method-assign]

        task = asyncio.create_task(supervised("test_merger_track", merger.run))
        await asyncio.sleep(0)
        task.cancel()

        await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True),
            timeout=_CANCEL_TIMEOUT_SECONDS,
        )

        assert flush_called, "_flush_all_buckets was NOT called during cancellation"

    async def test_flush_failure_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Flush failure during cancel must produce an ERROR log, not be silently swallowed."""
        merger = _make_merger()

        async def _raising_flush() -> None:
            raise RuntimeError("DB connection lost during shutdown")

        merger._flush_all_buckets = _raising_flush  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger="live.merge"):
            task = asyncio.create_task(supervised("test_merger_log", merger.run))
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True),
                timeout=_CANCEL_TIMEOUT_SECONDS,
            )

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, (
            "Expected at least one ERROR log when flush fails during cancellation; got none"
        )
        # The error must reference both the component and the underlying cause.
        combined = " ".join(r.getMessage() for r in error_records)
        assert "flush failed during shutdown" in combined, (
            f"ERROR log must describe the shutdown-flush failure; got: {combined!r}"
        )


# --------------------------------------------------------------------------
# PersistenceBatcher — cancel with failing flush
# --------------------------------------------------------------------------


class TestPersistenceCancelFlushError:
    """PersistenceBatcher.run(): CancelledError must propagate even when flush raises."""

    async def test_gather_completes_when_flush_raises(self) -> None:
        """asyncio.gather() must complete within the timeout even if _flush raises.

        Mirrors the merger regression test but exercises persistence.py:262.
        """
        batcher = _make_batcher()

        async def _raising_flush() -> None:
            raise RuntimeError("Postgres connection lost — simulated flush failure")

        batcher._flush = _raising_flush  # type: ignore[method-assign]

        task = asyncio.create_task(supervised("test_batcher", batcher.run))
        await asyncio.sleep(0)
        task.cancel()

        results = await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True),
            timeout=_CANCEL_TIMEOUT_SECONDS,
        )

        assert len(results) == 1
        assert isinstance(results[0], asyncio.CancelledError), (
            f"Expected CancelledError from gather; got {results[0]!r}"
        )

    async def test_flush_is_attempted_on_cancel(self) -> None:
        """_flush must be called during cancel even if the flush then raises."""
        batcher = _make_batcher()
        flush_called = False

        async def _tracking_flush() -> None:
            nonlocal flush_called
            flush_called = True
            raise RuntimeError("simulated failure after attempting flush")

        batcher._flush = _tracking_flush  # type: ignore[method-assign]

        task = asyncio.create_task(supervised("test_batcher_track", batcher.run))
        await asyncio.sleep(0)
        task.cancel()

        await asyncio.wait_for(
            asyncio.gather(task, return_exceptions=True),
            timeout=_CANCEL_TIMEOUT_SECONDS,
        )

        assert flush_called, "_flush was NOT called during cancellation"

    async def test_flush_failure_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Flush failure during cancel must produce an ERROR log, not be silently swallowed."""
        batcher = _make_batcher()

        async def _raising_flush() -> None:
            raise RuntimeError("Postgres connection timed out during shutdown")

        batcher._flush = _raising_flush  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR, logger="live.persistence"):
            task = asyncio.create_task(supervised("test_batcher_log", batcher.run))
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True),
                timeout=_CANCEL_TIMEOUT_SECONDS,
            )

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records, (
            "Expected at least one ERROR log when flush fails during cancellation; got none"
        )
        combined = " ".join(r.getMessage() for r in error_records)
        assert "flush failed during shutdown" in combined, (
            f"ERROR log must describe the shutdown-flush failure; got: {combined!r}"
        )

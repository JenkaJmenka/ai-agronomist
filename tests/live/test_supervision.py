"""Unit tests for src.live.supervision.supervised restart logic.

Covers:
  (a) Normal return (clean exit) triggers backoff sleep and restart — not a tight loop.
  (b) Exception triggers backoff sleep and restart with exponential growth.
  (c) Backoff resets to 1.0 s after a run longer than _BACKOFF_RESET_THRESHOLD,
      for both normal-exit and exception paths.
  (d) CancelledError propagates immediately and is never swallowed.
  (e) run_connector calls connect() then streams readings via submit().

asyncio.sleep is mocked so tests run in milliseconds rather than minutes.
The real asyncio.sleep is captured at module import time so both the mock's
yield-to-event-loop dance and coroutines that need genuine wall-clock waits
can still reach the original implementation without recursion.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.live.supervision import (
    _BACKOFF_RESET_THRESHOLD,
    _MAX_BACKOFF_SECONDS,
    run_connector,
    supervised,
)

# Capture the real function BEFORE any test patches it.
_REAL_SLEEP: Callable[..., object] = asyncio.sleep


def _make_fake_sleep(sleep_calls: list[float]) -> Callable[..., object]:
    """Return a coroutine function that records each delay then yields without waiting.

    Using _REAL_SLEEP(0) ensures we actually yield to the event loop so that
    other tasks (the supervised loop, cancellation requests, etc.) can run.
    The recorded *sleep_calls* list is shared by reference with the calling test.
    """

    async def _fake(delay: float) -> None:
        sleep_calls.append(delay)
        await _REAL_SLEEP(0)  # type: ignore[call-arg]  # yield without blocking

    return _fake


# ---------------------------------------------------------------------------
# (a) Normal return triggers backoff and restart
# ---------------------------------------------------------------------------


class TestNormalReturnTriggersBackoff:
    """coro_fn returning normally must sleep and restart — never a tight loop."""

    async def test_normal_return_sleeps_and_restarts(self) -> None:
        """Normal exit produces backoff sleep(s) and subsequent restarts."""
        call_count = 0
        stop_event = asyncio.Event()
        sleep_calls: list[float] = []

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                await stop_event.wait()
            # else: return normally → should trigger sleep + restart

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            task = asyncio.create_task(supervised("test_normal", coro))
            # Allow the first two normal exits + fake sleeps to complete, then
            # block on the event so we can assert state cleanly.
            await _REAL_SLEEP(0.05)  # type: ignore[call-arg]
            stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count >= 3, f"Expected >= 3 calls (restart after exit), got {call_count}"
        assert len(sleep_calls) >= 2, "Expected at least one sleep after each normal return"
        # Exponential backoff starts at 1.0, then doubles.
        assert sleep_calls[0] == 1.0, f"First backoff must be 1.0 s, got {sleep_calls[0]}"
        assert sleep_calls[1] == 2.0, f"Second backoff must double to 2.0 s, got {sleep_calls[1]}"

    async def test_normal_return_logs_clean_exit_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """A clean exit produces a warning log containing 'exited cleanly'."""
        call_count = 0
        stop_event = asyncio.Event()
        sleep_calls: list[float] = []

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                await stop_event.wait()

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            with caplog.at_level(logging.WARNING, logger="live.supervision"):
                task = asyncio.create_task(supervised("my_live_task", coro))
                await _REAL_SLEEP(0.05)  # type: ignore[call-arg]
                stop_event.set()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        matching = [
            r for r in caplog.records
            if "my_live_task" in r.message and "exited cleanly" in r.message
        ]
        assert matching, (
            f"Expected a 'exited cleanly' warning for 'my_live_task'; "
            f"records: {[r.message for r in caplog.records]}"
        )
        assert len(sleep_calls) >= 1, "Expected at least one backoff sleep"

    async def test_normal_return_backoff_doubles(self) -> None:
        """Each consecutive normal exit doubles the backoff up to _MAX_BACKOFF_SECONDS."""
        call_count = 0
        stop_event = asyncio.Event()
        sleep_calls: list[float] = []
        # We want at least 4 exits to verify growth: 1, 2, 4, 8.
        target_exits = 4

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count > target_exits:
                await stop_event.wait()

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            task = asyncio.create_task(supervised("test_doubles_normal", coro))
            await _REAL_SLEEP(0.1)  # type: ignore[call-arg]
            stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count > target_exits
        # Verify the first four sleeps are 1, 2, 4, 8.
        expected_prefix = [1.0, 2.0, 4.0, 8.0]
        for i, expected in enumerate(expected_prefix):
            if i < len(sleep_calls):
                assert sleep_calls[i] == expected, (
                    f"sleep_calls[{i}] = {sleep_calls[i]}, expected {expected}"
                )


# ---------------------------------------------------------------------------
# (b) Exception triggers backoff
# ---------------------------------------------------------------------------


class TestExceptionTriggersBackoff:
    """Unhandled exceptions must produce backoff sleep and restart."""

    async def test_exception_sleeps_and_restarts(self) -> None:
        """Exception causes backoff sleep then restart."""
        call_count = 0
        stop_event = asyncio.Event()
        sleep_calls: list[float] = []

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError(f"boom {call_count}")
            await stop_event.wait()

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            task = asyncio.create_task(supervised("test_exc", coro))
            await _REAL_SLEEP(0.05)  # type: ignore[call-arg]
            stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count >= 3
        assert sleep_calls[0] == 1.0, f"First backoff must be 1.0 s, got {sleep_calls[0]}"
        assert sleep_calls[1] == 2.0, f"Second backoff must be 2.0 s, got {sleep_calls[1]}"

    async def test_exception_logs_crashed_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """An exception produces a 'crashed' warning in the log."""
        call_count = 0
        stop_event = asyncio.Event()
        sleep_calls: list[float] = []

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("intentional test error")
            await stop_event.wait()

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            with caplog.at_level(logging.WARNING, logger="live.supervision"):
                task = asyncio.create_task(supervised("crash_task", coro))
                await _REAL_SLEEP(0.05)  # type: ignore[call-arg]
                stop_event.set()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        crash_records = [
            r for r in caplog.records
            if "crash_task" in r.message and "crashed" in r.message
        ]
        assert crash_records, (
            f"Expected a 'crashed' warning for 'crash_task'; "
            f"records: {[r.message for r in caplog.records]}"
        )

    async def test_backoff_capped_at_max(self) -> None:
        """Backoff stops doubling once _MAX_BACKOFF_SECONDS is reached."""
        call_count = 0
        stop_event = asyncio.Event()
        sleep_calls: list[float] = []

        # Need enough crashes to exceed the cap; log2(60) ≈ 6 doublings.
        num_crashes = 10

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= num_crashes:
                raise RuntimeError("crash")
            await stop_event.wait()

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            task = asyncio.create_task(supervised("test_cap", coro))
            await _REAL_SLEEP(0.1)  # type: ignore[call-arg]
            stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count > num_crashes
        # All recorded sleeps must not exceed _MAX_BACKOFF_SECONDS.
        for i, delay in enumerate(sleep_calls):
            assert delay <= _MAX_BACKOFF_SECONDS, (
                f"sleep_calls[{i}] = {delay} exceeds _MAX_BACKOFF_SECONDS={_MAX_BACKOFF_SECONDS}"
            )
        # Once the cap is reached it must stay there.
        capped = [d for d in sleep_calls if d == _MAX_BACKOFF_SECONDS]
        assert capped, "Expected at least one sleep at the cap"


# ---------------------------------------------------------------------------
# (c) Backoff resets after a run longer than _BACKOFF_RESET_THRESHOLD
# ---------------------------------------------------------------------------


class TestBackoffResetAfterLongRun:
    """After a run >= _BACKOFF_RESET_THRESHOLD, backoff resets to 1.0 s.

    _BACKOFF_RESET_THRESHOLD is patched to a tiny value so tests don't
    sleep for 120 s.  The coro uses _REAL_SLEEP (captured before patching)
    to perform a genuine wall-clock wait that exceeds the tiny threshold.
    """

    async def test_long_run_normal_exit_resets_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Normal exit after a long run resets backoff to 1.0 s before sleeping."""
        tiny_threshold = 0.005  # 5 ms — easy to exceed with _REAL_SLEEP
        monkeypatch.setattr("src.live.supervision._BACKOFF_RESET_THRESHOLD", tiny_threshold)

        call_count = 0
        stop_event = asyncio.Event()
        sleep_calls: list[float] = []

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("quick crash 1")  # backoff 1.0 → 2.0
            if call_count == 2:
                raise RuntimeError("quick crash 2")  # backoff 2.0 → 4.0
            if call_count == 3:
                # "Long run" — use the real sleep so wall-clock actually advances.
                await _REAL_SLEEP(tiny_threshold * 4)  # type: ignore[call-arg]
                return  # normal exit; run_duration >> threshold → reset to 1.0
            await stop_event.wait()

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            task = asyncio.create_task(supervised("test_reset_normal", coro))
            # Time budget: 2 fake sleeps (instant) + real 0.02 s + fake sleep + 4th call
            await _REAL_SLEEP(0.2)  # type: ignore[call-arg]
            stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count >= 4, f"Expected >= 4 calls, got {call_count}"
        # sleep after crash 1 → 1.0; sleep after crash 2 → 2.0; after long exit → reset 1.0
        assert sleep_calls[0] == 1.0, f"After crash 1 backoff must be 1.0 s; got {sleep_calls}"
        assert sleep_calls[1] == 2.0, f"After crash 2 backoff must be 2.0 s; got {sleep_calls}"
        assert sleep_calls[2] == 1.0, (
            f"After long normal exit, backoff must reset to 1.0 s (not 4.0); got {sleep_calls}"
        )

    async def test_long_run_exception_resets_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exception after a long run resets backoff to 1.0 s before sleeping."""
        tiny_threshold = 0.005
        monkeypatch.setattr("src.live.supervision._BACKOFF_RESET_THRESHOLD", tiny_threshold)

        call_count = 0
        stop_event = asyncio.Event()
        sleep_calls: list[float] = []

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("quick crash 1")  # ratchet backoff: 1.0 → 2.0
            if call_count == 2:
                raise RuntimeError("quick crash 2")  # ratchet backoff: 2.0 → 4.0
            if call_count == 3:
                await _REAL_SLEEP(tiny_threshold * 4)  # type: ignore[call-arg]
                raise RuntimeError("late crash after long run")  # reset → 1.0
            await stop_event.wait()

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            task = asyncio.create_task(supervised("test_reset_exc", coro))
            await _REAL_SLEEP(0.2)  # type: ignore[call-arg]
            stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count >= 4, f"Expected >= 4 calls, got {call_count}"
        assert sleep_calls[0] == 1.0, f"After crash 1 backoff must be 1.0 s; got {sleep_calls}"
        assert sleep_calls[1] == 2.0, f"After crash 2 backoff must be 2.0 s; got {sleep_calls}"
        assert sleep_calls[2] == 1.0, (
            f"After long-run crash, backoff must reset to 1.0 s (not 4.0); got {sleep_calls}"
        )

    async def test_short_run_does_not_reset_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quick exits must NOT reset backoff — only long runs do."""
        # Use a large threshold so no "quick" run ever resets.
        monkeypatch.setattr(
            "src.live.supervision._BACKOFF_RESET_THRESHOLD",
            999_999.0,  # practically never triggered
        )

        call_count = 0
        stop_event = asyncio.Event()
        sleep_calls: list[float] = []

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 4:
                raise RuntimeError("quick crash")
            await stop_event.wait()

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            task = asyncio.create_task(supervised("test_no_reset", coro))
            await _REAL_SLEEP(0.1)  # type: ignore[call-arg]
            stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Backoff must keep doubling: 1.0, 2.0, 4.0 — no resets.
        assert sleep_calls[:3] == [1.0, 2.0, 4.0], (
            f"Without reset, backoff must double; got {sleep_calls}"
        )


# ---------------------------------------------------------------------------
# (d) CancelledError propagates immediately
# ---------------------------------------------------------------------------


class TestCancelledErrorPropagates:
    """CancelledError must never be swallowed by supervised."""

    async def test_cancelled_during_coro_propagates(self) -> None:
        """CancelledError raised inside the coro propagates out of supervised.

        asyncio.Event().wait() is used so the block is not subject to the
        sleep patch (this test runs without patching sleep at all).
        """
        call_count = 0
        started = asyncio.Event()

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            started.set()
            await asyncio.Event().wait()  # blocks until task is cancelled

        task = asyncio.create_task(supervised("cancel_test", coro))
        await started.wait()  # ensure coro is running
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Task should have started exactly once and not retried.
        assert call_count == 1, f"CancelledError must not trigger restart; call_count={call_count}"

    async def test_cancelled_during_backoff_sleep_propagates(self) -> None:
        """CancelledError during the backoff sleep also propagates cleanly."""
        call_count = 0
        sleep_calls: list[float] = []
        # We want to cancel the task while it is inside the backoff asyncio.sleep.
        cancel_event = asyncio.Event()

        async def fake_sleep_then_signal(delay: float) -> None:
            sleep_calls.append(delay)
            cancel_event.set()          # signal that we're in the backoff sleep
            await _REAL_SLEEP(999)      # type: ignore[call-arg]  # will be cancelled

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("crash to enter backoff sleep")

        with patch("asyncio.sleep", new=fake_sleep_then_signal):
            task = asyncio.create_task(supervised("cancel_backoff_test", coro))
            await cancel_event.wait()   # coro crashed; we're now in backoff sleep
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

        assert call_count == 1, "Must not restart after cancellation during backoff sleep"
        assert len(sleep_calls) == 1, "Exactly one backoff sleep entered before cancel"

    async def test_cancelled_does_not_restart(self) -> None:
        """After cancellation, the supervised coroutine is not restarted.

        Uses asyncio.Event().wait() inside the coro so the block is not
        subject to the asyncio.sleep patch.
        """
        call_count = 0
        sleep_calls: list[float] = []
        started = asyncio.Event()

        async def coro() -> None:
            nonlocal call_count
            call_count += 1
            started.set()
            # Wait on a never-set event — blocks until the task is cancelled.
            await asyncio.Event().wait()

        with patch("asyncio.sleep", new=_make_fake_sleep(sleep_calls)):
            task = asyncio.create_task(supervised("no_restart_after_cancel", coro))
            await started.wait()  # ensure the coro has entered its wait
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # No backoff sleep should have been recorded (cancel happened inside the coro).
        assert sleep_calls == [], f"No backoff sleeps expected after cancel; got {sleep_calls}"
        assert call_count == 1, f"Coro must run exactly once; got {call_count}"


# ---------------------------------------------------------------------------
# (e) run_connector: connect + stream + submit
# ---------------------------------------------------------------------------


class TestRunConnector:
    """run_connector calls connector.connect(), then streams readings to merger.submit()."""

    async def test_connect_then_stream_readings(self) -> None:
        """Calls connect, iterates stream(), and submits each reading to the merger."""
        reading_1 = MagicMock(name="reading_1")
        reading_2 = MagicMock(name="reading_2")

        connector = MagicMock()
        connector.connect = AsyncMock()

        async def _fake_stream():
            yield reading_1
            yield reading_2

        connector.stream = _fake_stream

        merger = AsyncMock()

        await run_connector(connector, merger)

        connector.connect.assert_awaited_once()
        assert merger.submit.await_count == 2
        merger.submit.assert_any_await(reading_1)
        merger.submit.assert_any_await(reading_2)

    async def test_empty_stream(self) -> None:
        """When stream yields no readings, connect is still called and submit is not."""
        connector = MagicMock()
        connector.connect = AsyncMock()

        async def _empty_stream():
            return
            yield  # noqa: unreachable — makes this an async generator

        connector.stream = _empty_stream

        merger = AsyncMock()

        await run_connector(connector, merger)

        connector.connect.assert_awaited_once()
        merger.submit.assert_not_awaited()

    async def test_connect_exception_propagates(self) -> None:
        """If connect() raises, the exception propagates (not swallowed)."""
        connector = AsyncMock()
        connector.connect.side_effect = ConnectionError("refused")

        merger = AsyncMock()

        with pytest.raises(ConnectionError, match="refused"):
            await run_connector(connector, merger)

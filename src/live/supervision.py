"""Generic task supervision for the live monitoring pipeline.

Restart-on-crash wrappers with exponential back-off used by
:class:`~src.live.runtime.LiveRuntime` to keep long-running coroutines
(merger loops, connector streams, persistence flush) alive.  No
runtime-specific state lives here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.live.merge import ChannelMerger
from src.live.sources.base import LiveSourceConnector

logger = logging.getLogger("live.supervision")

# Exponential back-off cap for supervised restart loops (seconds).
_MAX_BACKOFF_SECONDS: float = 60.0

_BACKOFF_RESET_THRESHOLD: float = 2.0 * _MAX_BACKOFF_SECONDS
"""If a coroutine ran longer than this before failing, reset backoff to
the floor (1.0 s) — the long healthy run indicates a transient failure,
not a persistent crash loop."""


async def _supervised(label: str, coro_fn: Any, *args: Any) -> None:
    """Run *coro_fn* in a restart loop with exponential back-off.

    Propagates ``CancelledError`` immediately so the supervisor shuts down
    cleanly when the enclosing ``asyncio.Task`` is cancelled.

    Both clean exits (normal return) and crashes (unhandled exception) are
    treated as transient faults: the coroutine is restarted after sleeping
    the current backoff, which then doubles (capped at ``_MAX_BACKOFF_SECONDS``).

    Backoff resets to the floor (1.0 s) when the coroutine ran longer than
    ``_BACKOFF_RESET_THRESHOLD`` (2 × ``_MAX_BACKOFF_SECONDS``) before
    exiting — a long healthy run is transient, not a persistent fault.

    Parameters
    ----------
    label:
        Human-readable name used in log messages.
    coro_fn:
        Async callable to supervise.  Called with ``*args`` on each run.
    """
    backoff: float = 1.0
    while True:
        run_start = asyncio.get_event_loop().time()
        caught_exc: Exception | None = None
        try:
            await coro_fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            caught_exc = exc

        run_duration = asyncio.get_event_loop().time() - run_start
        if run_duration >= _BACKOFF_RESET_THRESHOLD:
            # Long healthy run before exit — treat as transient, reset backoff.
            backoff = 1.0

        if caught_exc is not None:
            logger.warning(
                "Live task %r crashed (%s) after %.1f s — restarting in %.1f s",
                label,
                caught_exc,
                run_duration,
                backoff,
            )
        else:
            logger.warning(
                "Live task %r exited cleanly after %.1f s — restarting in %.1f s",
                label,
                run_duration,
                backoff,
            )

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2.0, _MAX_BACKOFF_SECONDS)


async def _run_connector(
    connector: LiveSourceConnector,
    merger: ChannelMerger,
) -> None:
    """Connect to a data source and stream readings into the merger."""
    await connector.connect()
    async for reading in connector.stream():
        await merger.submit(reading)

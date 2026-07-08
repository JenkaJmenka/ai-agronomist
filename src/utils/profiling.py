"""Lightweight structured phase timing for the upload -> train -> find pipeline.

Emits greppable one-line records so per-phase wall-clock can be reconstructed
from platform logs (Railway / Render) without a metrics backend::

    PROFILE phase=upload.prepare.train ok=1 elapsed_ms=182340.5 job=abc rows=54000

Grep the platform logs with ``grep 'PROFILE ' <logs>`` to see where the minutes
go. Add per-block fields (row counts, portion index) by mutating the dict the
context manager yields.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

from src.config import PROFILE_LOGGING_ENABLED

# Distinctive prefix so a single grep isolates every timing record.
PROFILE_PREFIX = "PROFILE"


def _format(phase: str, elapsed_ms: float, ok: bool, fields: dict[str, Any]) -> str:
    parts = [f"phase={phase}", f"ok={int(ok)}", f"elapsed_ms={elapsed_ms:.1f}"]
    parts.extend(f"{key}={value}" for key, value in fields.items() if value is not None)
    return f"{PROFILE_PREFIX} " + " ".join(parts)


def profile_enabled() -> bool:
    """Whether PROFILE lines are emitted (gated by PROFILE_LOGGING_ENABLED)."""
    return PROFILE_LOGGING_ENABLED


def profile_event(logger: logging.Logger, phase: str, ok: bool = True, **fields: Any) -> None:
    """Emit a single point-in-time PROFILE record (no timing). Gated by the flag."""
    if PROFILE_LOGGING_ENABLED:
        parts = [f"phase={phase}", f"ok={int(ok)}"]
        parts.extend(f"{key}={value}" for key, value in fields.items() if value is not None)
        logger.info(f"{PROFILE_PREFIX} " + " ".join(parts))


@contextmanager
def timed(logger: logging.Logger, phase: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a synchronous block; log one PROFILE line on exit (success or error).

    Yields a mutable dict — assign extra fields inside the block (e.g.
    ``ctx["rows"] = len(df)``) and they are appended to the emitted record.
    """
    extra: dict[str, Any] = dict(fields)
    start = time.perf_counter()
    ok = True
    try:
        yield extra
    except BaseException:
        ok = False
        raise
    finally:
        if PROFILE_LOGGING_ENABLED:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            logger.info(_format(phase, elapsed_ms, ok, extra))


@asynccontextmanager
async def atimed(
    logger: logging.Logger, phase: str, **fields: Any
) -> AsyncIterator[dict[str, Any]]:
    """Async counterpart of :func:`timed` for blocks that ``await``."""
    extra: dict[str, Any] = dict(fields)
    start = time.perf_counter()
    ok = True
    try:
        yield extra
    except BaseException:
        ok = False
        raise
    finally:
        if PROFILE_LOGGING_ENABLED:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            logger.info(_format(phase, elapsed_ms, ok, extra))

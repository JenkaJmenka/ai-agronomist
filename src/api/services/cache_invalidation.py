"""Best-effort response-cache invalidation helper.

After any successful data mutation, call ``invalidate_response_cache`` to clear
the FastAPICache Redis backend.  The helper is deliberately best-effort:

- If FastAPICache has not been initialised (unit-test environments, early
  startup), the ``AttributeError`` is caught, a warning is logged, and
  ``False`` is returned.  The caller is never interrupted.
- Any other exception (Redis down, timeout, etc.) is handled the same way.

Design note: full ``FastAPICache.clear()`` is used rather than targeted key
deletion because ``request_scoped_key_builder`` keys are md5(route+URL) —
per-tenant invalidation is not feasible without a key registry.  The 300 s TTL
and low expected traffic make a full flush after each mutation acceptable.
"""

from __future__ import annotations

import logging

from fastapi_cache import FastAPICache

logger = logging.getLogger(__name__)


async def invalidate_response_cache(reason: str) -> bool:
    """Clear the FastAPICache Redis backend (best-effort).

    Args:
        reason: Short description of why the cache is being invalidated.
                Used only in the warning log on failure.

    Returns:
        ``True`` if the cache was cleared successfully, ``False`` otherwise.
        Never raises.
    """
    try:
        await FastAPICache.clear()
        return True
    except Exception:
        logger.warning(
            "Response cache invalidation failed (best-effort) — reason: %s",
            reason,
            exc_info=True,
        )
        return False

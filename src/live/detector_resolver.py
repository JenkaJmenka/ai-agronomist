"""Detector resolution for live scoring — the DI seam around DB + checkpoint.

``DetectorResolver`` owns "which detector should this tenant use right now".
It resolves the tenant's active deployment through
``src.api.services.detector_cache`` — the same single resolution path the
upload routes use — and caches the result, re-resolving every
``LIVE_DETECTOR_REFRESH_SECONDS``.

There is deliberately **no filesystem baseline fallback**. No baseline ships in
the container image (``.dockerignore`` excludes ``models/*.pt``,
``models/*.pth`` and ``models/checkpoints/``), so the previous
``AnomalyDetector(None)`` fallback could only ever raise ``FileNotFoundError``
for ``models/baseline.pt`` — a path that has never existed in any environment.
``detector_cache.resolve_tenant_detector`` already refused that fallback for
exactly this reason; the live path used to reintroduce it.

A tenant with no deployed model therefore resolves to ``None``, which
``LiveScorer`` treats as "live, not analysed": readings still persist, and the
scope reports ``detector_state="missing"`` on ``/api/live/status`` rather than
looking healthy.

Negative results are cached for ``LIVE_DETECTOR_MISSING_RETRY_SECONDS`` and
logged once per state *change*, not once per event. Before this, a failed
resolution returned without stamping ``_resolved_at``, so every live event
re-ran the registry lookup and another ``torch.load`` — measured on a 2 s mock
feed at 23.7 failed resolutions/min and 47.3 ``torch.load`` attempts/min, each
with a full traceback.

The ``session_factory`` and ``checkpoint_resolver`` collaborators are
injectable so unit tests can exercise resolution logic without a real database
or checkpoint file. Production defaults are imported lazily inside
:meth:`resolve` — the live package must not pull in the API/DB layers at import
time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from src.config import (
    LIVE_DETECTOR_MISSING_RETRY_SECONDS,
    LIVE_DETECTOR_REFRESH_SECONDS,
)

if TYPE_CHECKING:
    from src.models.inference import AnomalyDetector

logger = logging.getLogger(__name__)

# Reported verbatim as ``detector_state`` on GET /api/live/status.
DETECTOR_STATE_UNKNOWN = "unknown"
DETECTOR_STATE_READY = "ready"
DETECTOR_STATE_MISSING = "missing"
DETECTOR_STATE_ERROR = "error"

DetectorState = str


class DetectorResolver:
    """Resolves (and caches) the active detector for one tenant.

    Parameters
    ----------
    tenant_id:
        Tenant whose active model should be resolved.
    session_factory:
        Async-session context-manager factory (production default:
        ``src.db.session.AsyncSessionLocal``).
    checkpoint_resolver:
        ``(session, tenant_id) -> checkpoint_path`` coroutine (production
        default: ``detector_cache.resolve_tenant_checkpoint``). Kept separate
        from the detector load so the DB work can be awaited while the
        synchronous ``torch.load`` runs in a worker thread.
    detector_loader:
        ``checkpoint_path -> AnomalyDetector`` callable (production default:
        ``detector_cache.get_cached_detector``). Invoked via
        ``asyncio.to_thread`` because the first load per checkpoint runs
        ``torch.load``, which would otherwise block the event loop and stall
        the live hub.
    """

    def __init__(
        self,
        tenant_id: str,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        checkpoint_resolver: Optional[Callable[..., Awaitable[Optional[str]]]] = None,
        detector_loader: Optional[Callable[[Optional[str]], "AnomalyDetector"]] = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._session_factory = session_factory
        self._checkpoint_resolver = checkpoint_resolver
        self._detector_loader = detector_loader
        self._detector: Optional[AnomalyDetector] = None
        self._resolved_at: float = 0.0
        self._state: DetectorState = DETECTOR_STATE_UNKNOWN
        # Monotonic stamp of the last ATTEMPT, success or failure. The negative
        # cache keys off this; _resolved_at keys off success only.
        self._attempted_at: float = 0.0

    @property
    def state(self) -> DetectorState:
        """``unknown`` before the first attempt, then ``ready``/``missing``/``error``."""
        return self._state

    @property
    def has_detector(self) -> bool:
        return self._detector is not None

    async def resolve(self) -> Optional["AnomalyDetector"]:
        """Return the tenant's detector, or ``None`` when none is deployed.

        Never raises. ``None`` means "do not score this event"; the caller
        persists the reading unscored.
        """
        now = time.monotonic()

        if (
            self._detector is not None
            and (now - self._resolved_at) < LIVE_DETECTOR_REFRESH_SECONDS
        ):
            return self._detector

        # Negative cache. Without this, a scope with no deployed model re-runs
        # the registry lookup and another torch.load on EVERY event.
        if (
            self._detector is None
            and self._attempted_at > 0.0
            and (now - self._attempted_at) < LIVE_DETECTOR_MISSING_RETRY_SECONDS
        ):
            return None

        self._attempted_at = now
        try:
            detector = await self._resolve_once()
        except Exception as exc:
            self._detector = None
            self._transition(self._classify(exc), exc)
            return None

        self._detector = detector
        self._resolved_at = now
        self._transition(DETECTOR_STATE_READY, None)
        return self._detector

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _resolve_once(self) -> "AnomalyDetector":
        """One full resolution: DB lookup awaited, torch.load off the loop."""
        session_factory = self._session_factory or self._default_session_factory()
        checkpoint_resolver = self._checkpoint_resolver or self._default_checkpoint_resolver()
        detector_loader = self._detector_loader or self._default_detector_loader()

        async with session_factory() as session:
            checkpoint_path = await checkpoint_resolver(session, self._tenant_id)

        return await asyncio.to_thread(detector_loader, checkpoint_path)

    @staticmethod
    def _classify(exc: BaseException) -> DetectorState:
        """``missing`` for "no model deployed"; ``error`` for anything else.

        The distinction matters to the operator: ``missing`` is a provisioning
        state that resolves by deploying a model, while ``error`` means the
        lookup itself is broken (DB unreachable, corrupt checkpoint) and is an
        incident.
        """
        from src.api.services.detector_cache import NoActiveModelError

        return (
            DETECTOR_STATE_MISSING
            if isinstance(exc, NoActiveModelError)
            else DETECTOR_STATE_ERROR
        )

    def _transition(self, state: DetectorState, exc: Optional[BaseException]) -> None:
        """Log only when the state actually changes — never once per event."""
        if state == self._state:
            return
        previous, self._state = self._state, state

        if state == DETECTOR_STATE_READY:
            logger.info(
                "Live detector resolved for %s (was %s)", self._tenant_id, previous
            )
        elif state == DETECTOR_STATE_MISSING:
            # Not an error: a tenant with no uploaded data legitimately has no
            # model. Retried every LIVE_DETECTOR_MISSING_RETRY_SECONDS.
            logger.warning(
                "No detector deployed for %s — live readings will be persisted "
                "unscored and /api/live/status reports detector_state=missing. "
                "Retrying every %.0fs. Cause: %s",
                self._tenant_id,
                LIVE_DETECTOR_MISSING_RETRY_SECONDS,
                exc,
            )
        else:
            logger.error(
                "Live detector resolution failed for %s (state %s -> %s); readings "
                "persist unscored, retrying every %.0fs",
                self._tenant_id,
                previous,
                state,
                LIVE_DETECTOR_MISSING_RETRY_SECONDS,
                exc_info=exc,
            )

    @staticmethod
    def _default_session_factory() -> Callable[[], Any]:
        from src.db.session import AsyncSessionLocal
        return AsyncSessionLocal

    @staticmethod
    def _default_checkpoint_resolver() -> Callable[..., Awaitable[Optional[str]]]:
        from src.api.services.detector_cache import resolve_tenant_checkpoint
        return resolve_tenant_checkpoint

    @staticmethod
    def _default_detector_loader() -> Callable[[Optional[str]], "AnomalyDetector"]:
        from src.api.services.detector_cache import get_cached_detector
        return get_cached_detector

"""Detector resolution for live scoring — the DI seam around DB + checkpoint.

``DetectorResolver`` owns "which detector should this tenant use right now":
it looks up the tenant's active model in the registry, resolves the
checkpoint, and loads a cached :class:`~src.models.inference.AnomalyDetector`,
re-resolving every ``_DETECTOR_REFRESH_SECONDS``.

The ``session_factory`` and ``detector_loader`` collaborators are injectable
so unit tests can exercise resolution logic without a real database or
checkpoint file.  Production defaults (``AsyncSessionLocal`` and
``get_cached_detector``) are imported lazily inside :meth:`resolve` — the
live package must not pull in the API/DB layers at import time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from src.models.inference import AnomalyDetector

logger = logging.getLogger(__name__)

# How often (seconds, monotonic) to re-resolve the tenant's checkpoint.
_DETECTOR_REFRESH_SECONDS: float = 300.0


class DetectorResolver:
    """Resolves (and caches) the active detector for one tenant.

    Parameters
    ----------
    tenant_id:
        Tenant whose active model should be resolved.
    session_factory:
        Async-session context-manager factory (production default:
        ``src.db.session.AsyncSessionLocal``).
    detector_loader:
        ``checkpoint_path -> AnomalyDetector`` callable (production default:
        ``src.api.services.detector_cache.get_cached_detector``).  Invoked in
        a worker thread via ``asyncio.to_thread`` because the first load per
        checkpoint runs ``torch.load``, which would otherwise block the
        event loop.
    """

    def __init__(
        self,
        tenant_id: str,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        detector_loader: Optional[Callable[[Optional[str]], "AnomalyDetector"]] = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._session_factory = session_factory
        self._detector_loader = detector_loader
        self._detector: Optional[AnomalyDetector] = None
        self._resolved_at: float = 0.0

    async def resolve(self) -> Optional["AnomalyDetector"]:
        """Lazy resolution with 5-minute refresh; falls back to baseline.

        Returns ``None`` only when both the tenant's checkpoint and the
        baseline detector fail to load.
        """
        now = time.monotonic()
        if (
            self._detector is not None
            and (now - self._resolved_at) < _DETECTOR_REFRESH_SECONDS
        ):
            return self._detector

        try:
            from src.models.artifact_store import resolve_checkpoint
            from src.models.model_registry import ModelRegistry

            session_factory = self._session_factory or self._default_session_factory()
            detector_loader = self._detector_loader or self._default_detector_loader()

            async with session_factory() as session:
                registry = ModelRegistry(session)
                active = await registry.get_active_model(self._tenant_id)
                cp = active["checkpoint_path"] if active else None
                cp = await resolve_checkpoint(session, cp)

            self._detector = await asyncio.to_thread(detector_loader, cp)
            self._resolved_at = now
        except Exception:
            logger.warning(
                "Detector resolution failed for %s, trying baseline",
                self._tenant_id, exc_info=True,
            )
            try:
                detector_loader = self._detector_loader or self._default_detector_loader()
                self._detector = await asyncio.to_thread(detector_loader, None)
                self._resolved_at = now
            except Exception:
                logger.error(
                    "Baseline detector also failed for %s",
                    self._tenant_id, exc_info=True,
                )
                return None

        return self._detector

    @staticmethod
    def _default_session_factory() -> Callable[[], Any]:
        from src.db.session import AsyncSessionLocal
        return AsyncSessionLocal

    @staticmethod
    def _default_detector_loader() -> Callable[[Optional[str]], "AnomalyDetector"]:
        from src.api.services.detector_cache import get_cached_detector
        return get_cached_detector

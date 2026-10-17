"""LRU cache for loaded AnomalyDetector instances, plus tenant resolution.

``resolve_tenant_detector`` is the single place that turns "which model
should this tenant's request use" into a loaded, cached ``AnomalyDetector``.
It mirrors the resolution the /inference routes do inline (ModelRegistry
lookup, then artifact_store.resolve_checkpoint for db:// paths) so callers
outside inference.py — e.g. the backfill and comprehensive-inference
endpoints — don't grow yet another copy of that glue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import MAX_PIPELINE_CACHE_SIZE
from src.models.artifact_store import resolve_checkpoint
from src.models.inference import AnomalyDetector
from src.models.model_registry import ModelRegistry
from src.utils.lru import LRUDict

_BASELINE_CACHE_KEY = "__baseline__"
_detector_cache: LRUDict = LRUDict(max_size=MAX_PIPELINE_CACHE_SIZE)


class NoActiveModelError(RuntimeError):
    """No usable model could be resolved for a tenant."""


@dataclass(frozen=True)
class ResolvedDetector:
    """A loaded detector plus the resolved checkpoint path that produced it.

    Callers that keep their own secondary cache on top of this one (e.g. the
    ComprehensiveInferencePipeline cache in inference_integrated.py) need a
    stable, collision-free key. ``checkpoint_path`` here is post-resolution
    (a real local path, never a raw ``db://`` reference): resolve_checkpoint
    is content-addressed, so two different db:// artifacts always resolve to
    two different local paths, and the same artifact always resolves to the
    same one — unlike the raw registry value, which was the bug this type
    exists to prevent from recurring.
    """

    detector: AnomalyDetector
    checkpoint_path: Optional[str]


def get_cached_detector(checkpoint_path: Optional[str]) -> AnomalyDetector:
    """Return a cached detector, loading and storing on first use."""
    key = checkpoint_path or _BASELINE_CACHE_KEY
    try:
        return _detector_cache[key]
    except KeyError:
        detector = AnomalyDetector(checkpoint_path=checkpoint_path)
        _detector_cache[key] = detector
        return detector


async def resolve_tenant_checkpoint(
    db: AsyncSession,
    tenant_id: str,
    explicit_checkpoint_path: Optional[str] = None,
) -> Optional[str]:
    """Resolve the checkpoint path a tenant is actually running.

    An *explicit_checkpoint_path*, when given, wins outright — but still goes
    through ``resolve_checkpoint`` so a caller may pass a ``db://`` artifact
    path, not only a local file. Otherwise the tenant's active
    ``ModelRegistry`` deployment is used.

    Raises ``NoActiveModelError`` rather than returning ``None`` for "no model
    deployed". There is no filesystem baseline to degrade to: worker-trained
    checkpoints live in the DB artifact store on deployments with no shared
    filesystem, and no baseline ships in the container image at all
    (``.dockerignore`` excludes ``models/*.pt`` and ``models/checkpoints/``),
    so a silent fallback would just relocate this class of bug.

    Split out of :func:`resolve_tenant_detector` so the live path can await the
    DB work here and then run the synchronous ``torch.load`` in a worker
    thread, instead of blocking the event loop and stalling the live hub.
    """
    if explicit_checkpoint_path is not None:
        checkpoint_path: Optional[str] = explicit_checkpoint_path
    else:
        active = await ModelRegistry(db).get_active_model(tenant_id)
        if active is None:
            raise NoActiveModelError(
                f"No active model is deployed for tenant {tenant_id!r} and no "
                "checkpoint_path was supplied. Tried "
                "ModelRegistry.get_active_model(tenant_id)."
            )
        checkpoint_path = active["checkpoint_path"]

    try:
        return await resolve_checkpoint(db, checkpoint_path)
    except FileNotFoundError as exc:
        raise NoActiveModelError(
            f"Model checkpoint for tenant {tenant_id!r} could not be resolved "
            f"({checkpoint_path!r}): {exc}"
        ) from exc


async def resolve_tenant_detector(
    db: AsyncSession,
    tenant_id: str,
    explicit_checkpoint_path: Optional[str] = None,
) -> ResolvedDetector:
    """Resolve the model a tenant is actually running and return a detector.

    Thin wrapper over :func:`resolve_tenant_checkpoint` — see it for the
    resolution order and why there is no baseline fallback.
    """
    checkpoint_path = await resolve_tenant_checkpoint(
        db, tenant_id, explicit_checkpoint_path
    )
    return ResolvedDetector(
        detector=get_cached_detector(checkpoint_path),
        checkpoint_path=checkpoint_path,
    )


def clear_detector_cache() -> None:
    """Reset the detector cache (primarily for tests)."""
    _detector_cache.data.clear()
    _detector_cache.usage.clear()

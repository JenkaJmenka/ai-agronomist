"""Anomaly enrichment for live scoring — fire-and-forget, supervised.

``EnrichmentRunner`` owns the set of in-flight enrichment tasks spawned when
an anomaly episode starts.  Each task runs the comprehensive inference
pipeline (LLM reasoning + ``Incident`` persistence) and publishes an
``anomaly-detail`` event correlated to the episode by ``anomaly_seq``.

Enrichment must NEVER crash or slow the hot scoring path — every exception
is logged and swallowed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Set

import numpy as np

from src.live.detector_resolver import DetectorResolver
from src.live.events import AnomalyDetailEvent
from src.live.hub import LiveHub
from src.models.data_utils import SENSOR_RANGES, normalize_data

logger = logging.getLogger(__name__)


class EnrichmentRunner:
    """Owns fire-and-forget enrichment tasks for one tenant/greenhouse scope."""

    def __init__(
        self,
        *,
        tenant_id: str,
        greenhouse_id: str,
        hub: LiveHub,
        resolver: DetectorResolver,
    ) -> None:
        self._tenant_id = tenant_id
        self._greenhouse_id = greenhouse_id
        self._hub = hub
        self._resolver = resolver
        self._tasks: Set[asyncio.Task[None]] = set()

    @property
    def tasks(self) -> Set[asyncio.Task[None]]:
        """In-flight enrichment tasks (introspection for tests/monitoring)."""
        return self._tasks

    def spawn(self, window_array: np.ndarray, anomaly_seq: str) -> None:
        """Launch a supervised enrichment task for a new episode."""
        task = asyncio.create_task(
            self._run_enrichment(window_array.copy(), anomaly_seq),
        )
        self._tasks.add(task)
        task.add_done_callback(self._on_enrichment_done)

    async def close(self) -> None:
        """Cancel and await all in-flight enrichment tasks."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _on_enrichment_done(self, task: asyncio.Task[None]) -> None:
        """Clean up after an enrichment task completes."""
        self._tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("Enrichment task raised: %s", exc)

    async def _run_enrichment(
        self, window_array: np.ndarray, anomaly_seq: str,
    ) -> None:
        """Run ``ComprehensiveInferencePipeline``, persist ``Incident``, publish detail.

        Any exception is logged and swallowed — enrichment must NEVER crash
        or slow the hot scoring path.
        """
        try:
            detector = await self._resolver.resolve()
            if detector is None:
                return

            normalized = normalize_data(window_array, SENSOR_RANGES)

            from src.api.services.comprehensive_pipeline import (
                ComprehensiveInferencePipeline,
            )
            from src.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                pipeline = ComprehensiveInferencePipeline(detector=detector)
                result = await pipeline.run(
                    customer_id=self._tenant_id,
                    sequence=normalized.tolist(),
                    db=session,
                    greenhouse_id=self._greenhouse_id,
                )

            # Extract incident_id from the pipeline result (UUID string).
            incident_id: Optional[str] = None
            incident = result.get("incident")
            if incident is not None and hasattr(incident, "id"):
                incident_id = str(incident.id)
            elif isinstance(result.get("incident_id"), str):
                incident_id = result["incident_id"]

            await self._hub.publish(
                self._tenant_id,
                self._greenhouse_id,
                "anomaly-detail",
                AnomalyDetailEvent(
                    anomaly_seq=anomaly_seq,
                    tenant_id=self._tenant_id,
                    greenhouse_id=self._greenhouse_id,
                    reasoning=result.get("reasoning", ""),
                    fault_type=result.get("fault_type"),
                    root_cause=result.get("root_cause"),
                    incident_id=incident_id,
                ),
            )
        except Exception:
            logger.warning(
                "Enrichment failed for %s/%s (non-critical)",
                self._tenant_id, self._greenhouse_id, exc_info=True,
            )

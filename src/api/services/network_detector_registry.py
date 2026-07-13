"""Per-tenant registries for stateful OT network detectors.

RTT/jitter needs the previous RTT sample, the service-response dynamic-P99
threshold needs an accumulated sample window, and the Isolation Forest
threat detector needs its fitted model — instantiating a detector per
request resets all three, which left those mechanisms dead in the API
path. Endpoints share per-tenant instances from here instead.

State is per-process (mirrors ``orchestrator_registry``): deploy the API
single-worker or move to a shared store before scaling out.
"""

from __future__ import annotations

from typing import Optional

from src.config import NETWORK_REGISTRY_MAX_TENANTS
from src.models.network.isolation_forest_threat_detector import (
    IsolationForestThreatDetector,
)
from src.models.network.rtt_jitter_detector import RTTJitterDetector
from src.models.network.service_response_detector import ServiceResponseDetector
from src.utils.lru import LRUDict

_rtt_detectors: LRUDict = LRUDict(max_size=NETWORK_REGISTRY_MAX_TENANTS)
_service_detectors: LRUDict = LRUDict(max_size=NETWORK_REGISTRY_MAX_TENANTS)
_threat_detectors: LRUDict = LRUDict(max_size=NETWORK_REGISTRY_MAX_TENANTS)


def get_rtt_detector(tenant_id: str) -> RTTJitterDetector:
    """Shared per-tenant RTT/jitter detector (keeps previous-RTT state)."""
    detector = _rtt_detectors.get(tenant_id)
    if detector is None:
        detector = RTTJitterDetector()
        _rtt_detectors[tenant_id] = detector
    return detector


def get_service_detector(tenant_id: str) -> ServiceResponseDetector:
    """Shared per-tenant service-response detector (accumulates the P99 window)."""
    detector = _service_detectors.get(tenant_id)
    if detector is None:
        detector = ServiceResponseDetector()
        _service_detectors[tenant_id] = detector
    return detector


def get_threat_detector(tenant_id: str) -> Optional[IsolationForestThreatDetector]:
    """Fitted per-tenant threat detector, or None until /threat/fit ran."""
    return _threat_detectors.get(tenant_id)


def set_threat_detector(
    tenant_id: str, detector: IsolationForestThreatDetector
) -> None:
    _threat_detectors[tenant_id] = detector


def clear_network_registries() -> None:
    """Reset all registries (test isolation)."""
    _rtt_detectors.clear()
    _service_detectors.clear()
    _threat_detectors.clear()

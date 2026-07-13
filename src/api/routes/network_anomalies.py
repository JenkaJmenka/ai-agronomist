"""OT network security anomaly detection endpoints.

Provides REST endpoints for real-time network telemetry checks
(RTT/jitter, IAT CV, TCP handshake, DHCP fingerprint, service
response, and Isolation Forest threat scoring).

RTT/jitter, service-response, and threat scoring keep per-tenant state
(previous RTT, rolling P99 window, fitted forest) via
``network_detector_registry`` — a fresh detector per request would reset
those mechanisms.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.api.services.network_detector_registry import (
    get_rtt_detector,
    get_service_detector,
    get_threat_detector,
    set_threat_detector,
)
from src.models.network.dhcp_fingerprint_detector import DHCPFingerprintDetector
from src.models.network.iat_cv_detector import IATCVDetector
from src.models.network.isolation_forest_threat_detector import (
    IsolationForestThreatDetector,
)
from src.models.network.tcp_handshake_detector import TCPHandshakeDetector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/network", tags=["network-anomalies"])


class RTTCheckRequest(BaseModel):
    rtt_ms: float = Field(..., description="Current round-trip time in ms")
    previous_rtt_ms: Optional[float] = Field(None, description="Previous RTT for jitter")
    tenant_id: str = Field("default", description="Tenant whose RTT stream this is")


class RTTCheckResponse(BaseModel):
    is_anomaly: bool
    rtt_violation: bool
    jitter_violation: bool
    rtt_ms: float
    jitter_ms: Optional[float]


class TCPCheckRequest(BaseModel):
    handshake_ms: float = Field(..., description="SYN-to-SYN-ACK duration in ms")


class DHCPCheckRequest(BaseModel):
    params: List[str] = Field(..., description="Ordered config strings (MAC, GW, DNS, ...)")
    baseline_hash: str = Field(..., description="Enrolled baseline SHA-256")


class ServiceCheckRequest(BaseModel):
    duration_ms: float = Field(..., description="Response time in ms")
    baseline_threshold_ms: Optional[float] = Field(None, description="Override threshold")
    tenant_id: str = Field("default", description="Tenant whose latency stream this is")


class IATCheckRequest(BaseModel):
    iat_series: List[float] = Field(
        ..., min_length=1, description="Inter-packet arrival intervals in seconds"
    )


class IATCheckResponse(BaseModel):
    is_anomaly: bool
    flags: List[bool]


class ThreatFitRequest(BaseModel):
    tenant_id: str = Field("default")
    vectors: List[List[float]] = Field(
        ..., min_length=2, description="Normal-traffic feature vectors (n x 19)"
    )


class ThreatFitResponse(BaseModel):
    fitted_samples: int


class ThreatScoreRequest(BaseModel):
    tenant_id: str = Field("default")
    vector: List[float] = Field(..., description="Current feature vector (19 features)")


class ThreatScoreResponse(BaseModel):
    threat_score: float
    is_anomaly: bool


class AnomalyFlagResponse(BaseModel):
    is_anomaly: bool
    detail: Optional[str] = None


@router.post("/rtt-jitter", response_model=RTTCheckResponse)
def check_rtt_jitter(req: RTTCheckRequest) -> RTTCheckResponse:
    """Check a single RTT measurement for latency and jitter anomalies.

    The per-tenant detector remembers the previous RTT, so jitter works
    across consecutive requests; ``previous_rtt_ms`` overrides it.
    """
    detector = get_rtt_detector(req.tenant_id)
    result = detector.detect(req.rtt_ms, req.previous_rtt_ms)
    return RTTCheckResponse(
        is_anomaly=result.is_anomaly,
        rtt_violation=result.rtt_violation,
        jitter_violation=result.jitter_violation,
        rtt_ms=result.rtt_ms,
        jitter_ms=result.jitter_ms,
    )


@router.post("/iat", response_model=IATCheckResponse)
def check_iat(req: IATCheckRequest) -> IATCheckResponse:
    """Check an inter-packet interval series for CV / spacing anomalies."""
    flags = IATCVDetector().detect(np.asarray(req.iat_series, dtype=float))
    return IATCheckResponse(is_anomaly=any(flags), flags=flags)


@router.post("/tcp-handshake", response_model=AnomalyFlagResponse)
def check_tcp_handshake(req: TCPCheckRequest) -> AnomalyFlagResponse:
    """Check a TCP handshake latency."""
    detector = TCPHandshakeDetector()
    flagged = detector.detect(req.handshake_ms)
    return AnomalyFlagResponse(
        is_anomaly=flagged,
        detail=f"Latency {req.handshake_ms}ms exceeds threshold {detector.threshold_ms}ms"
        if flagged
        else None,
    )


@router.post("/dhcp-fingerprint", response_model=AnomalyFlagResponse)
def check_dhcp_fingerprint(req: DHCPCheckRequest) -> AnomalyFlagResponse:
    """Validate DHCP configuration fingerprint against baseline."""
    current_hash = DHCPFingerprintDetector.compute_hash(*req.params)
    flagged = DHCPFingerprintDetector.detect(current_hash, req.baseline_hash)
    return AnomalyFlagResponse(
        is_anomaly=flagged,
        detail=f"Hash mismatch: {current_hash[:16]}... vs baseline {req.baseline_hash[:16]}..."
        if flagged
        else None,
    )


@router.post("/service-response", response_model=AnomalyFlagResponse)
def check_service_response(req: ServiceCheckRequest) -> AnomalyFlagResponse:
    """Check a service response duration against the dynamic P99 threshold.

    Every observed duration feeds the per-tenant rolling window (after
    detection, so a sample never softens its own check); the threshold is
    P99 of that window with the static baseline as a floor.
    """
    detector = get_service_detector(req.tenant_id)
    flagged = detector.detect(req.duration_ms, req.baseline_threshold_ms)
    detector.update_baseline(req.duration_ms)
    return AnomalyFlagResponse(
        is_anomaly=flagged,
        detail=(
            f"Duration {req.duration_ms}ms exceeds threshold "
            f"{detector.threshold_ms:.1f}ms"
        )
        if flagged
        else None,
    )


@router.post("/threat/fit", response_model=ThreatFitResponse)
def fit_threat_detector(req: ThreatFitRequest) -> ThreatFitResponse:
    """Fit the per-tenant Isolation Forest on normal-traffic vectors."""
    vectors = np.asarray(req.vectors, dtype=float)
    if vectors.ndim != 2:
        raise HTTPException(
            status_code=422, detail="vectors must be a 2-D array (n_samples x n_features)"
        )
    detector = IsolationForestThreatDetector()
    detector.fit(vectors)
    set_threat_detector(req.tenant_id, detector)
    logger.info(
        "Fitted threat detector for tenant %s on %d vectors", req.tenant_id, len(vectors)
    )
    return ThreatFitResponse(fitted_samples=len(vectors))


@router.post("/threat/score", response_model=ThreatScoreResponse)
def score_threat(req: ThreatScoreRequest) -> ThreatScoreResponse:
    """Score a feature vector against the tenant's fitted Isolation Forest."""
    detector = get_threat_detector(req.tenant_id)
    if detector is None:
        raise HTTPException(
            status_code=409,
            detail=f"No fitted threat detector for tenant {req.tenant_id!r} — "
            "POST /api/network/threat/fit first",
        )
    threat_score, is_anomaly = detector.detect(np.asarray(req.vector, dtype=float))
    return ThreatScoreResponse(threat_score=threat_score, is_anomaly=is_anomaly)

"""OT network security anomaly detection endpoints.

Provides REST endpoints for real-time network telemetry checks
(RTT/jitter, IAT CV, TCP handshake, DHCP fingerprint, service
response, and Isolation Forest threat scoring).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.models.network.dhcp_fingerprint_detector import DHCPFingerprintDetector
from src.models.network.rtt_jitter_detector import RTTJitterDetector
from src.models.network.service_response_detector import ServiceResponseDetector
from src.models.network.tcp_handshake_detector import TCPHandshakeDetector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/network", tags=["network-anomalies"])


class RTTCheckRequest(BaseModel):
    rtt_ms: float = Field(..., description="Current round-trip time in ms")
    previous_rtt_ms: Optional[float] = Field(None, description="Previous RTT for jitter")


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


class AnomalyFlagResponse(BaseModel):
    is_anomaly: bool
    detail: Optional[str] = None


@router.post("/rtt-jitter", response_model=RTTCheckResponse)
def check_rtt_jitter(req: RTTCheckRequest) -> RTTCheckResponse:
    """Check a single RTT measurement for latency and jitter anomalies."""
    detector = RTTJitterDetector()
    result = detector.detect(req.rtt_ms, req.previous_rtt_ms)
    return RTTCheckResponse(
        is_anomaly=result.is_anomaly,
        rtt_violation=result.rtt_violation,
        jitter_violation=result.jitter_violation,
        rtt_ms=result.rtt_ms,
        jitter_ms=result.jitter_ms,
    )


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
    """Check a service response duration against threshold."""
    detector = ServiceResponseDetector()
    flagged = detector.detect(req.duration_ms, req.baseline_threshold_ms)
    return AnomalyFlagResponse(
        is_anomaly=flagged,
        detail=f"Duration {req.duration_ms}ms exceeds threshold"
        if flagged
        else None,
    )

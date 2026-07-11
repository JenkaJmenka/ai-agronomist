"""Multi-dimensional Isolation Forest threat scoring for OT networks.

Processes a 19-feature network behaviour vector (RTT, jitter, IAT CV,
RSSI, BSSID counts, DNS latency, etc.) through an ensemble of
isolation trees.  The anomaly score is converted to a 0-100 threat
score; values above the threshold trigger an alert.
"""

from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
from sklearn.ensemble import IsolationForest as SklearnIF

from src.config import (
    IF_THREAT_CONTAMINATION,
    IF_THREAT_N_ESTIMATORS,
    THREAT_SCORE_THRESHOLD,
)


class IsolationForestThreatDetector:
    """Multi-feature Isolation Forest for OT network threat detection.

    Feature-vector contract (19 features, fixed order — spec §15, audit §15):
        Indices 0-2 are anchored by the spec's Table 16 verification vector;
        the remaining features are additional OT-network behavioural signals.
        The spec enumerates these only partially ("RTT, jitter, IAT CV, RSSI,
        BSSID counts, DNS latency, ..."), so indices 6-18 below follow that
        enumeration and are not byte-for-byte pinned by the spec text.

            0   rtt_ms                RTT / gateway latency (ms)
            1   jitter_ms             |R_t - R_{t-1}| jitter (ms)
            2   iat_cv                Inter-packet interval CV
            3   rssi_dbm              Signal strength (dBm)
            4   bssid_count           Distinct BSSIDs observed
            5   dns_latency_ms        DNS resolution latency (ms)
            6   tcp_handshake_ms      SYN -> SYN-ACK time (ms)
            7   service_response_ms   Microservice response duration (ms)
            8   packet_rate           Packets per second
            9   byte_rate             Bytes per second
            10  retransmit_ratio      TCP retransmission ratio
            11  syn_ratio             SYN / total-packet ratio
            12  unique_dst_ports      Distinct destination ports
            13  arp_rate              ARP requests per second
            14  dhcp_renew_rate       DHCP renew events per hour
            15  icmp_ratio            ICMP / total-packet ratio
            16  tls_handshake_ms      TLS handshake time (ms)
            17  conn_duration_s       Mean connection duration (s)
            18  entropy_payload       Payload byte-entropy

    The threat score is ``s(x, psi) = 2^(-E[h(x)]/c(psi))`` (via sklearn's
    ``score_samples``) rescaled to 0-100; an alert fires when it exceeds the
    configured ``threshold`` (spec §15: ``theta_threat = 70.0``).
    """

    def __init__(
        self,
        n_estimators: int = IF_THREAT_N_ESTIMATORS,
        contamination: float = IF_THREAT_CONTAMINATION,
        # spec ref: theta_threat=70.0 (docs/anomaly_framework_spec.md §15);
        # production default THREAT_SCORE_THRESHOLD=65.0 is more sensitive.
        threshold: float = THREAT_SCORE_THRESHOLD,
        random_state: int = 42,
    ) -> None:
        self._threshold = threshold
        self._model: Optional[SklearnIF] = None
        self._n_estimators = n_estimators
        self._contamination = contamination
        self._random_state = random_state

    def fit(self, baseline_features: np.ndarray) -> None:
        """Fit the Isolation Forest on normal-traffic feature vectors.

        Args:
            baseline_features: Array of shape ``(n_samples, n_features)``.
        """
        self._model = SklearnIF(
            n_estimators=self._n_estimators,
            contamination=self._contamination,
            random_state=self._random_state,
        )
        self._model.fit(baseline_features)

    def detect(
        self,
        feature_vector: np.ndarray,
    ) -> Tuple[float, bool]:
        """Score a single feature vector.

        Args:
            feature_vector: 1-D array of shape ``(n_features,)`` or
                2-D of shape ``(1, n_features)``.

        Returns:
            ``(threat_score, is_anomaly)`` where threat_score is in
            [0, 100] and is_anomaly is ``True`` when it exceeds the
            configured threshold.

        Raises:
            RuntimeError: If the model has not been fitted.
        """
        if self._model is None:
            raise RuntimeError("Model not fitted — call fit() first")

        vec = np.asarray(feature_vector, dtype=float)
        if vec.ndim == 1:
            vec = vec.reshape(1, -1)

        raw_score = -self._model.score_samples(vec)[0]
        threat_score = float(np.clip(raw_score * 100, 0, 100))

        return threat_score, threat_score > self._threshold

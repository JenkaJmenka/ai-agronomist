"""Model representing a detected anomaly."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Anomaly:
    """Anomaly represents a detected anomalous event with its metadata, confidence, and context."""

    timestamp: str
    sensor: str
    fault_type: str
    confidence: float
    physics_valid: bool
    detector_agreement: int
    season: str
    details: List[Dict] = field(default_factory=list)
    explanation: Optional[Dict] = None
    cluster_id: Optional[str] = None
    cluster_severity: Optional[str] = None

    def to_dict(self) -> Dict:
        """Convert the Anomaly instance to a dictionary representation, recursively serializing explanation."""
        exp = self.explanation
        if exp is not None:
            if hasattr(exp, "to_dict"):
                exp = exp.to_dict()
            elif hasattr(exp, "__dataclass_fields__"):
                from dataclasses import asdict
                exp = asdict(exp)

        return {
            "timestamp": self.timestamp,
            "sensor": self.sensor,
            "fault_type": self.fault_type,
            "confidence": self.confidence,
            "physics_valid": self.physics_valid,
            "detector_agreement": self.detector_agreement,
            "season": self.season,
            "details": self.details,
            "explanation": exp,
            "cluster_id": self.cluster_id,
            "cluster_severity": self.cluster_severity,
        }

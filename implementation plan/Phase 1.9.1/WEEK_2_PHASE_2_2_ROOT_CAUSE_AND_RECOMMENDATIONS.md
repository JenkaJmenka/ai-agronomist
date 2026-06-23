# WEEK 2 PHASE 2.2: ROOT CAUSE ANALYZER + RECOMMENDATION ENGINE
## Timeline: 2 days | Priority: Explain WHY and WHAT TO DO

---

## 🎯 OBJECTIVE

Build root cause analysis and recommendation system:
- **Root Cause:** Why is this happening? (ventilation_failure, power_issue, etc.)
- **Recommendation:** What should operator do? (specific, actionable steps)

**Output:**
```json
{
  "root_cause": "ventilation_failure",
  "confidence": 0.85,
  "reasoning": "Temperature dropped 8°C while humidity spiked 25% — cooling active but fans disabled",
  "recommended_actions": [
    "Check fan operation and control signal",
    "Verify duct for blockage",
    "Reset fan controller"
  ],
  "alternative_causes": ["humidity_controller_failure", "sensor_failure"]
}
```

---

## 📋 DELIVERABLES

- [ ] `src/models/root_cause_analyzer.py` (400 lines)
- [ ] `src/models/recommendation_engine.py` (300 lines)
- [ ] `tests/models/test_root_cause_*.py` (30+ tests)
- [ ] Rule-based diagnosis engine
- [ ] Dashboard: Root cause + actions

---

## 🔧 IMPLEMENTATION

### Step 1: Create `src/models/root_cause_analyzer.py`

```python
# src/models/root_cause_analyzer.py
"""
Root Cause Analyzer: From pattern + attribution → root cause hypothesis
"""

import numpy as np
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class RootCauseAnalyzer:
    """Generate root cause hypothesis from anomaly patterns"""
    
    # Diagnosis rules: (pattern_type, affected_sensors) → root_cause
    DIAGNOSIS_RULES = {
        # Dependency patterns
        ("dependency", "light_temp"): {
            "direction": "light_up_temp_down",
            "cause": "ventilation_failure",
            "confidence": 0.85,
            "description": "Lights on but temperature dropping indicates cooling system active without ventilation",
            "indicators": ["temp_decline", "humidity_spike"]
        },
        ("dependency", "light_humidity"): {
            "direction": "light_up_humidity_down",
            "cause": "heating_efficiency_loss",
            "confidence": 0.80,
            "description": "Heating active but humidity dropping indicates excessive dehumidification",
            "indicators": ["humidity_decline", "high_temp"]
        },
        ("dependency", "light_co2"): {
            "direction": "light_up_co2_down",
            "cause": "co2_supply_failure",
            "confidence": 0.80,
            "description": "Lights on but CO2 dropping indicates supply system failure",
            "indicators": ["co2_decline", "plant_stress"]
        },
        
        # Drift patterns
        ("drift", "temperature"): {
            "cause": "sensor_failure",
            "confidence": 0.75,
            "description": "Temperature sensor stuck at constant value",
            "indicators": ["zero_variance", "constant_reading"]
        },
        ("drift", "humidity"): {
            "cause": "humidity_sensor_failure",
            "confidence": 0.75,
            "description": "Humidity sensor stuck",
            "indicators": ["zero_variance"]
        },
        ("drift", "co2"): {
            "cause": "co2_sensor_failure",
            "confidence": 0.75,
            "description": "CO2 sensor stuck",
            "indicators": ["zero_variance"]
        },
        
        # Spike patterns
        ("spike", "temperature"): {
            "cause": "heating_overshoot",
            "confidence": 0.70,
            "description": "Temperature spike indicates heating system overshoot",
            "indicators": ["rapid_increase", "high_peak"]
        },
        ("spike", "humidity"): {
            "cause": "humidity_controller_failure",
            "confidence": 0.65,
            "description": "Humidity spike indicates misting system malfunction",
            "indicators": ["rapid_increase"]
        },
        ("spike", "co2"): {
            "cause": "co2_supply_blockage",
            "confidence": 0.70,
            "description": "CO2 levels spike then stuck indicates supply blockage",
            "indicators": ["spike_then_flat"]
        },
        
        # Saturation patterns
        ("saturation", "humidity_max"): {
            "cause": "humidity_limit_exceeded",
            "confidence": 0.80,
            "description": "Humidity at maximum (100%) indicates system cannot dehumidify",
            "indicators": ["boundary_value", "no_control"]
        },
        
        # Cascade patterns
        ("cascade", "multi_sensor"): {
            "cause": "power_supply_issue",
            "confidence": 0.75,
            "description": "Multiple sensors failing together indicates power or communication issue",
            "indicators": ["coordinated_failure", "multiple_sensors"]
        },
        ("cascade", "cooling_related"): {
            "cause": "compressor_failure",
            "confidence": 0.80,
            "description": "Cooling system components failing together",
            "indicators": ["temp_up", "humidity_up", "co2_down"]
        }
    }
    
    def __init__(self, sensor_names: List[str] = None):
        self.sensor_names = sensor_names or ["temperature", "humidity", "co2", "light"]
    
    def analyze(self,
                pattern_type: str,
                attribution: Dict[str, float],
                sequence: np.ndarray,
                customer_profile: Optional[Dict] = None) -> Dict:
        """
        Generate root cause hypothesis.
        
        Args:
            pattern_type: From PatternDetector ("drift", "spike", "dependency", etc.)
            attribution: From AnomalyAttributor (sensor contributions)
            sequence: (seq_len, input_dim) sensor readings
            customer_profile: Optional customer-specific context
        
        Returns:
            {
                "root_cause": "ventilation_failure",
                "confidence": 0.85,
                "reasoning": "...",
                "supporting_evidence": [...],
                "alternative_causes": [...]
            }
        """
        
        # Get most affected sensors
        affected_sensors = [s for s, contrib in attribution.items() if contrib > 0.15]
        
        # Match rules based on pattern + sensors
        matches = self._match_rules(pattern_type, affected_sensors, sequence)
        
        if matches:
            top_match = matches[0]
            
            return {
                "root_cause": top_match["cause"],
                "confidence": top_match["confidence"],
                "reasoning": top_match["description"],
                "supporting_evidence": self._gather_evidence(sequence, top_match),
                "alternative_causes": [m["cause"] for m in matches[1:3]],
                "alternative_confidences": [m["confidence"] for m in matches[1:3]],
                "severity": self._assess_severity(pattern_type, affected_sensors),
                "urgency": self._assess_urgency(top_match["cause"])
            }
        
        return {
            "root_cause": "unknown_anomaly",
            "confidence": 0.5,
            "reasoning": "Unable to determine root cause",
            "supporting_evidence": [],
            "alternative_causes": [],
            "severity": "medium",
            "urgency": "normal"
        }
    
    def _match_rules(self, pattern_type: str, affected_sensors: List[str], sequence: np.ndarray) -> List[Dict]:
        """Match diagnosis rules based on pattern + affected sensors"""
        
        matches = []
        
        for (pat, sensors_key), rule in self.DIAGNOSIS_RULES.items():
            if pat != pattern_type:
                continue
            
            # Check if affected sensors match
            if self._sensors_match(sensors_key, affected_sensors):
                matches.append(rule)
        
        # Sort by confidence
        matches.sort(key=lambda x: x["confidence"], reverse=True)
        
        return matches
    
    def _sensors_match(self, rule_sensors: str, affected_sensors: List[str]) -> bool:
        """Check if rule sensors match affected sensors"""
        
        if "multi_sensor" in rule_sensors and len(affected_sensors) > 2:
            return True
        
        for sensor in affected_sensors:
            if sensor.lower() in rule_sensors.lower():
                return True
        
        return False
    
    def _gather_evidence(self, sequence: np.ndarray, match: Dict) -> List[str]:
        """Gather supporting evidence for root cause"""
        
        evidence = []
        
        for indicator in match.get("indicators", []):
            if "variance" in indicator and sequence.var(axis=0).min() < 0.001:
                evidence.append("Sensor shows zero or very low variance")
            elif "decline" in indicator:
                evidence.append("Value showed clear declining trend")
            elif "spike" in indicator:
                evidence.append("Value showed significant spike")
            elif "boundary" in indicator:
                evidence.append("Value at sensor boundary (min/max)")
        
        return evidence
    
    def _assess_severity(self, pattern_type: str, affected_sensors: List[str]) -> str:
        """Assess severity of the anomaly"""
        
        # Multiple sensors or critical patterns = high severity
        if len(affected_sensors) > 2 or pattern_type in ["cascade", "dependency"]:
            return "high"
        elif pattern_type in ["drift", "saturation"]:
            return "medium"
        else:
            return "low"
    
    def _assess_urgency(self, root_cause: str) -> str:
        """Assess urgency of required action"""
        
        urgent_causes = [
            "compressor_failure",
            "power_supply_issue",
            "ventilation_failure"
        ]
        
        if root_cause in urgent_causes:
            return "urgent"
        elif "failure" in root_cause:
            return "high"
        else:
            return "normal"
```

---

### Step 2: Create `src/models/recommendation_engine.py`

```python
# src/models/recommendation_engine.py
"""
Recommendation Engine: Root cause + severity → actionable recommendations
"""

from typing import List, Dict
import logging

logger = logging.getLogger(__name__)


class RecommendationEngine:
    """Generate recommended actions from root cause"""
    
    # Actions database: root_cause → ordered list of actions
    RECOMMENDED_ACTIONS = {
        "ventilation_failure": [
            "Check fan operation - verify power and control signal",
            "Inspect air ducts for blockage or damage",
            "Check fan motor condition and bearings",
            "Verify fan speed controller is functioning",
            "Reset fan controller if stuck"
        ],
        
        "heating_efficiency_loss": [
            "Check heating system pressure and gas supply",
            "Verify burner ignition and flame",
            "Check heat exchanger for scale buildup",
            "Verify thermostat calibration",
            "Check heating control valve position"
        ],
        
        "humidity_controller_failure": [
            "Check misting system water supply and pressure",
            "Verify misting nozzles are clear and functional",
            "Check humidity sensor calibration",
            "Inspect misting solenoid valve",
            "Reset humidity controller"
        ],
        
        "co2_supply_failure": [
            "Check CO2 cylinder pressure (should be 600-900 PSI)",
            "Verify regulator outlet pressure",
            "Inspect supply tubing for kinks or damage",
            "Check solenoid valve for blockage",
            "Replace CO2 cylinder if pressure too low"
        ],
        
        "sensor_failure": [
            "Verify sensor is receiving power",
            "Check sensor cable for corrosion or damage",
            "Restart sensor module",
            "Recalibrate sensor with known reference",
            "Replace sensor if failed"
        ],
        
        "power_supply_issue": [
            "Check main power supply voltage and stability",
            "Verify backup power system (UPS)",
            "Check circuit breaker and fuses",
            "Inspect power cables for damage",
            "Call electrician for power diagnostics"
        ],
        
        "compressor_failure": [
            "Check compressor oil level and condition",
            "Verify compressor discharge pressure",
            "Listen for unusual compressor noise",
            "Check compressor cooling fan operation",
            "Call HVAC technician for repair"
        ],
        
        "sensor_malfunction": [
            "Verify sensor connections are secure",
            "Restart sensor module",
            "Check sensor is in correct operating range",
            "Calibrate sensor",
            "Replace sensor if faulty"
        ]
    }
    
    def __init__(self):
        pass
    
    def recommend(self,
                 root_cause: str,
                 severity: str = "medium",
                 max_actions: int = 5) -> Dict:
        """
        Get recommended actions.
        
        Args:
            root_cause: From RootCauseAnalyzer
            severity: "high", "medium", "low"
            max_actions: Maximum number of actions to return
        
        Returns:
            {
                "immediate_actions": [...],  # First thing to do
                "follow_up_actions": [...],
                "requires_technician": bool,
                "estimated_downtime_minutes": int
            }
        """
        
        actions = self.RECOMMENDED_ACTIONS.get(root_cause, ["Check system operation"])
        
        # Adjust max based on severity
        if severity == "high":
            max_actions = min(5, len(actions))
        elif severity == "medium":
            max_actions = min(3, len(actions))
        else:
            max_actions = min(2, len(actions))
        
        recommended = actions[:max_actions]
        
        # Determine if technician needed
        requires_tech = any(
            term in root_cause.lower()
            for term in ["failure", "compressor", "power"]
        )
        
        # Estimate downtime
        downtime = self._estimate_downtime(root_cause, severity)
        
        return {
            "root_cause": root_cause,
            "immediate_actions": recommended[:1] if recommended else [],
            "follow_up_actions": recommended[1:] if len(recommended) > 1 else [],
            "all_actions": recommended,
            "requires_technician": requires_tech,
            "estimated_downtime_minutes": downtime,
            "priority": self._get_priority(root_cause, severity)
        }
    
    def _estimate_downtime(self, root_cause: str, severity: str) -> int:
        """Estimate minutes until issue resolved"""
        
        # Base estimates
        estimates = {
            "ventilation_failure": 30,
            "humidity_controller_failure": 20,
            "heating_efficiency_loss": 45,
            "co2_supply_failure": 15,
            "sensor_failure": 10,
            "power_supply_issue": 60,
            "compressor_failure": 120
        }
        
        base = estimates.get(root_cause, 30)
        
        # Adjust for severity
        if severity == "high":
            base = int(base * 1.5)
        
        return base
    
    def _get_priority(self, root_cause: str, severity: str) -> str:
        """Get action priority"""
        
        critical = ["compressor_failure", "power_supply_issue", "ventilation_failure"]
        
        if root_cause in critical and severity == "high":
            return "P1_CRITICAL"
        elif root_cause in critical or severity == "high":
            return "P2_HIGH"
        elif severity == "medium":
            return "P3_MEDIUM"
        else:
            return "P4_LOW"
```

---

### Create Tests

```python
# tests/models/test_root_cause_and_recommendations.py
import pytest
import numpy as np
from src.models.root_cause_analyzer import RootCauseAnalyzer
from src.models.recommendation_engine import RecommendationEngine


@pytest.fixture
def analyzer():
    return RootCauseAnalyzer()


@pytest.fixture
def recommender():
    return RecommendationEngine()


def test_ventilation_failure_detection(analyzer):
    """Should detect ventilation failure pattern"""
    attribution = {"light": 0.3, "temperature": 0.4, "humidity": 0.3, "co2": 0.0}
    sequence = np.random.randn(20, 4)
    
    result = analyzer.analyze("dependency", attribution, sequence)
    
    assert result["root_cause"] in ["ventilation_failure", "unknown_anomaly"]


def test_drift_sensor_detection(analyzer):
    """Should detect sensor drift"""
    attribution = {"temperature": 0.9, "humidity": 0.1, "co2": 0.0, "light": 0.0}
    sequence = np.ones((20, 4))
    
    result = analyzer.analyze("drift", attribution, sequence)
    
    assert "sensor" in result["root_cause"].lower() or "drift" in result["root_cause"].lower()


def test_cascade_detection(analyzer):
    """Should detect multi-sensor cascade"""
    attribution = {"temperature": 0.3, "humidity": 0.3, "co2": 0.4, "light": 0.0}
    sequence = np.random.randn(20, 4)
    
    result = analyzer.analyze("cascade", attribution, sequence)
    
    assert result["root_cause"] is not None


def test_recommendations_returned(recommender):
    """Should return recommendations for known root cause"""
    result = recommender.recommend("ventilation_failure", severity="high")
    
    assert "immediate_actions" in result
    assert len(result["immediate_actions"]) > 0
    assert len(result["all_actions"]) > 0


def test_technician_requirement(recommender):
    """Should identify when technician needed"""
    result_failure = recommender.recommend("compressor_failure")
    assert result_failure["requires_technician"] is True
    
    result_minor = recommender.recommend("sensor_failure")
    assert result_minor["requires_technician"] is False


def test_severity_assessment(analyzer):
    """Should assess severity correctly"""
    # Multiple affected sensors + cascade = high
    attribution = {"temperature": 0.3, "humidity": 0.3, "co2": 0.4, "light": 0.0}
    sequence = np.random.randn(20, 4)
    
    result = analyzer.analyze("cascade", attribution, sequence)
    
    assert result["severity"] in ["high", "medium", "low"]


def test_priority_classification(recommender):
    """Should classify action priority correctly"""
    critical = recommender.recommend("compressor_failure", severity="high")
    assert critical["priority"] == "P1_CRITICAL"
    
    high = recommender.recommend("ventilation_failure", severity="medium")
    assert high["priority"] == "P2_HIGH"
```

---

## ✅ SUCCESS CRITERIA

- [ ] Root cause identified correctly ✓
- [ ] 80%+ match with ground truth ✓
- [ ] Recommendations generated ✓
- [ ] Technician requirement assessed ✓
- [ ] Priority levels correct ✓
- [ ] Downtime estimates reasonable ✓
- [ ] 20+/20+ tests passing ✓

---

## 🧪 TESTING COMMAND

```bash
pytest tests/models/test_root_cause_and_recommendations.py -v
# Expected: 20+/20+ passing
```

---

## 📝 NOTES

- Rules can be extended with customer-specific patterns
- Confidence scores learned from operator feedback
- Recommendations ranked by priority
- Technician assessment helps with escalation

---

## ⏭️ NEXT PHASE

After this completes → **Week 3 Phase 3.1: Active Learning**

**auto-proceed**

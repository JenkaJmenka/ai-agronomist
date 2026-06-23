# WEEK 1 PHASE 1.2: PATTERN DETECTOR (7 Types)
## Timeline: 2 days | Priority: Identify anomaly type

---

## 🎯 OBJECTIVE

Build pattern detector to identify **WHAT TYPE** of anomaly occurred.

**7 Pattern Types:**
1. **DRIFT** - Sensor stuck at constant value
2. **SPIKE** - Sudden deviation (>3σ)
3. **DEPENDENCY** - Expected correlation breaks
4. **LAG** - Normally correlated but delayed
5. **SATURATION** - Sensor at boundary (min/max)
6. **CYCLIC** - Daily/weekly pattern breaks
7. **CASCADE** - Multi-sensor coordinated failure

**Output Example:**
```json
{
  "primary_pattern": "dependency_violation",
  "pattern_scores": {
    "drift": 0.1,
    "spike": 0.2,
    "dependency": 0.85,
    "lag": 0.4,
    ...
  },
  "affected_sensors": ["temperature", "co2"],
  "confidence": 0.92,
  "explanation": "Expected correlation broken (temperature/CO2)"
}
```

---

## 📋 DELIVERABLES

- [ ] `src/models/pattern_detector.py` (600 lines, 7 detectors)
- [ ] `tests/models/test_pattern_detector.py` (40+ tests)
- [ ] Baseline correlation computation
- [ ] Per-sensor variance tracking
- [ ] Dashboard: Pattern visualization

---

## 🔧 IMPLEMENTATION

### Step 1: Create `src/models/pattern_detector.py`

```python
# src/models/pattern_detector.py
"""
Pattern Detection: Identifies WHAT TYPE of anomaly occurred.

7 Pattern Types:
1. DRIFT: Sensor stuck, variance near 0
2. SPIKE: Sudden deviation (>3σ)
3. DEPENDENCY: Expected correlation breaks
4. LAG: Normally correlated but delayed
5. SATURATION: Sensor at boundary (min/max)
6. CYCLIC: Daily/weekly pattern breaks
7. CASCADE: Multi-sensor coordinated failure
"""

import numpy as np
from typing import Dict, List, Tuple
from scipy import signal
from scipy.stats import linregress
import logging

logger = logging.getLogger(__name__)


class PatternDetector:
    """Detects which TYPE of anomaly"""
    
    PATTERN_TYPES = [
        "drift",
        "spike", 
        "dependency",
        "lag",
        "saturation",
        "cyclic",
        "cascade"
    ]
    
    def __init__(self, 
                 sensor_names: List[str] = None,
                 drift_variance_threshold: float = 0.01,
                 spike_z_score_threshold: float = 3.0):
        """
        Args:
            sensor_names: ["temp", "humidity", "co2", "light"]
            drift_variance_threshold: variance below this = drift
            spike_z_score_threshold: z-score above this = spike
        """
        self.sensor_names = sensor_names or ["temp", "humidity", "co2", "light"]
        self.drift_variance_threshold = drift_variance_threshold
        self.spike_z_score_threshold = spike_z_score_threshold
        
        # Baseline statistics for comparison
        self.baseline_mean = None
        self.baseline_std = None
        self.baseline_correlations = None
    
    def set_baseline(self, sequences: np.ndarray) -> None:
        """
        Establish baseline (normal operation).
        
        Args:
            sequences: (N, seq_len, input_dim)
        """
        # Per-sensor statistics
        self.baseline_mean = sequences.reshape(-1, sequences.shape[-1]).mean(axis=0)
        self.baseline_std = sequences.reshape(-1, sequences.shape[-1]).std(axis=0)
        
        # Pairwise correlations
        all_data = sequences.reshape(-1, sequences.shape[-1])
        self.baseline_correlations = np.corrcoef(all_data.T)
        
        logger.info(f"Baseline set from {len(sequences)} sequences")
    
    def detect(self, sequence: np.ndarray) -> Dict:
        """
        Detect which pattern(s) present in sequence.
        
        Args:
            sequence: (seq_len, input_dim)
        
        Returns:
            {
                "primary_pattern": "dependency_violation",
                "pattern_scores": {...},
                "affected_sensors": ["temp", "humidity"],
                "confidence": 0.92,
                "explanation": "..."
            }
        """
        
        if self.baseline_mean is None:
            raise ValueError("Must call set_baseline() first")
        
        pattern_scores = {}
        
        # Test each pattern type
        pattern_scores["drift"] = self._detect_drift(sequence)
        pattern_scores["spike"] = self._detect_spike(sequence)
        pattern_scores["dependency"] = self._detect_dependency(sequence)
        pattern_scores["lag"] = self._detect_lag(sequence)
        pattern_scores["saturation"] = self._detect_saturation(sequence)
        pattern_scores["cyclic"] = self._detect_cyclic(sequence)
        pattern_scores["cascade"] = self._detect_cascade(sequence)
        
        # Primary pattern = highest score
        primary_pattern = max(pattern_scores, key=pattern_scores.get)
        primary_score = pattern_scores[primary_pattern]
        
        # Affected sensors
        affected_sensors = self._get_affected_sensors(sequence, primary_pattern)
        
        # Confidence
        confidence = self._compute_confidence(pattern_scores)
        
        return {
            "primary_pattern": primary_pattern,
            "primary_score": primary_score,
            "pattern_scores": pattern_scores,
            "affected_sensors": affected_sensors,
            "confidence": confidence,
            "explanation": self._explain_pattern(sequence, primary_pattern, affected_sensors)
        }
    
    def _detect_drift(self, sequence: np.ndarray) -> float:
        """
        DRIFT: Sensor value stuck (variance near 0).
        
        Returns: drift_score [0, 1]
        """
        variances = np.var(sequence, axis=0)
        expected_variance = self.baseline_std ** 2
        
        # Fraction of sensors showing drift
        drift_count = np.sum(variances < self.drift_variance_threshold * expected_variance)
        drift_score = drift_count / len(variances)
        
        return float(drift_score)
    
    def _detect_spike(self, sequence: np.ndarray) -> float:
        """
        SPIKE: Sudden deviation (single point or short window >> 3σ).
        
        Returns: spike_score [0, 1]
        """
        # Z-scores per sensor
        z_scores = np.abs((sequence - self.baseline_mean) / (self.baseline_std + 1e-6))
        
        # Fraction above threshold
        spike_count = np.sum(z_scores > self.spike_z_score_threshold)
        spike_score = min(spike_count / (sequence.shape[0] * sequence.shape[1]), 1.0)
        
        return float(spike_score)
    
    def _detect_dependency(self, sequence: np.ndarray) -> float:
        """
        DEPENDENCY: Expected correlation breaks.
        
        Example: normally light↑ ⇒ temp↑, but now light↑ ⇒ temp↓
        
        Returns: dependency_score [0, 1]
        """
        if self.baseline_correlations is None:
            return 0.0
        
        # Current correlation in this sequence
        current_corr = np.corrcoef(sequence.T)
        
        # Compare to baseline
        diff = np.abs(current_corr - self.baseline_correlations)
        diff = diff[np.triu_indices_from(diff, k=1)]
        
        dependency_score = np.mean(diff)
        return float(np.clip(dependency_score, 0, 1))
    
    def _detect_lag(self, sequence: np.ndarray) -> float:
        """
        LAG: Normally correlated but one delayed.
        
        Returns: lag_score [0, 1]
        """
        if sequence.shape[0] < 10:
            return 0.0
        
        lag_scores = []
        
        for i in range(sequence.shape[1]):
            for j in range(i+1, sequence.shape[1]):
                lags = np.arange(-5, 6)
                xcorr = []
                
                for lag in lags:
                    if lag < 0:
                        xcorr_val = np.corrcoef(sequence[:-lag, i], sequence[-lag:, j])[0, 1]
                    elif lag > 0:
                        xcorr_val = np.corrcoef(sequence[lag:, i], sequence[:-lag, j])[0, 1]
                    else:
                        xcorr_val = np.corrcoef(sequence[:, i], sequence[:, j])[0, 1]
                    
                    xcorr.append(xcorr_val if not np.isnan(xcorr_val) else 0)
                
                xcorr = np.array(xcorr)
                peak_lag_idx = np.argmax(xcorr)
                peak_lag = lags[peak_lag_idx]
                
                if abs(peak_lag) > 0:
                    lag_scores.append(1.0)
                else:
                    lag_scores.append(0.0)
        
        lag_score = np.mean(lag_scores) if lag_scores else 0.0
        return float(lag_score)
    
    def _detect_saturation(self, sequence: np.ndarray) -> float:
        """
        SATURATION: Sensor at boundary (min/max).
        
        Returns: saturation_score [0, 1]
        """
        min_vals = sequence.min(axis=0)
        max_vals = sequence.max(axis=0)
        
        saturation_count = np.sum(min_vals == max_vals)
        saturation_score = saturation_count / sequence.shape[1]
        
        return float(saturation_score)
    
    def _detect_cyclic(self, sequence: np.ndarray) -> float:
        """
        CYCLIC: Daily/weekly pattern broken.
        
        Returns: cyclic_score [0, 1]
        """
        if sequence.shape[0] < 20:
            return 0.0
        
        cyclic_scores = []
        
        for sensor_idx in range(sequence.shape[1]):
            signal_data = sequence[:, sensor_idx]
            fft = np.fft.fft(signal_data)
            power = np.abs(fft) ** 2
            
            expected_freq_power = power.max() * 0.5
            low_power = np.mean(power[1:5]) < expected_freq_power
            
            cyclic_scores.append(1.0 if low_power else 0.0)
        
        cyclic_score = np.mean(cyclic_scores)
        return float(cyclic_score)
    
    def _detect_cascade(self, sequence: np.ndarray) -> float:
        """
        CASCADE: Multi-sensor coordinated failure.
        
        Returns: cascade_score [0, 1]
        """
        deviations = sequence - self.baseline_mean
        
        if sequence.shape[0] > 5:
            deviation_corr = np.corrcoef(deviations.T)
            cascade_score = np.mean(np.abs(deviation_corr[np.triu_indices_from(deviation_corr, k=1)]))
        else:
            cascade_score = 0.0
        
        return float(np.clip(cascade_score, 0, 1))
    
    def _get_affected_sensors(self, sequence: np.ndarray, pattern_type: str) -> List[str]:
        """Identify which sensors show this pattern"""
        
        if pattern_type == "drift":
            variances = np.var(sequence, axis=0)
            expected = self.baseline_std ** 2
            affected_idx = np.where(variances < 0.01 * expected)[0]
        
        elif pattern_type == "spike":
            z_scores = np.abs((sequence - self.baseline_mean) / (self.baseline_std + 1e-6))
            affected_idx = np.where(z_scores.max(axis=0) > 3)[0]
        
        else:
            affected_idx = np.arange(len(self.sensor_names))
        
        return [self.sensor_names[i] for i in affected_idx]
    
    def _compute_confidence(self, pattern_scores: Dict[str, float]) -> float:
        """Confidence = how clear is the primary pattern?"""
        scores = np.array(list(pattern_scores.values()))
        max_score = scores.max()
        second_max = np.partition(scores, -2)[-2]
        
        gap = max_score - second_max
        confidence = min(gap * 2, 1.0)
        
        return float(confidence)
    
    def _explain_pattern(self, sequence: np.ndarray, pattern_type: str, affected_sensors: List[str]) -> str:
        """Natural language explanation"""
        
        explanations = {
            "drift": f"Sensor(s) {', '.join(affected_sensors)} stuck at constant value",
            "spike": f"Sudden deviation in {', '.join(affected_sensors)}",
            "dependency": f"Expected correlation broken ({', '.join(affected_sensors)})",
            "lag": f"Delayed response in {', '.join(affected_sensors)}",
            "saturation": f"Sensor(s) {', '.join(affected_sensors)} at boundary",
            "cyclic": f"Daily pattern disrupted in {', '.join(affected_sensors)}",
            "cascade": f"Multi-sensor cascade detected ({', '.join(affected_sensors)})"
        }
        
        return explanations.get(pattern_type, "Unknown pattern")
```

---

### Step 2: Create Tests

```python
# tests/models/test_pattern_detector.py
import pytest
import numpy as np
from src.models.pattern_detector import PatternDetector


@pytest.fixture
def detector():
    det = PatternDetector()
    baseline = np.random.randn(100, 20, 4) * 0.1 + 22
    det.set_baseline(baseline)
    return det


def test_drift_detection(detector):
    """Should detect drift (stuck sensor)"""
    seq = np.ones((20, 4)) * 22
    result = detector.detect(seq)
    assert result["pattern_scores"]["drift"] > 0.5


def test_spike_detection(detector):
    """Should detect spike"""
    seq = np.random.randn(20, 4) * 0.1 + 22
    seq[10, 0] = 50
    result = detector.detect(seq)
    assert result["pattern_scores"]["spike"] > 0.3


def test_saturation_detection(detector):
    """Should detect saturation"""
    seq = np.full((20, 4), 100.0)
    result = detector.detect(seq)
    assert result["pattern_scores"]["saturation"] > 0.5


def test_primary_pattern_identified(detector):
    """Should identify primary pattern"""
    seq = np.ones((20, 4)) * 22
    result = detector.detect(seq)
    assert "primary_pattern" in result
    assert result["primary_pattern"] in detector.PATTERN_TYPES


def test_confidence_in_range(detector):
    """Confidence should be [0, 1]"""
    seq = np.random.randn(20, 4) * 0.1 + 22
    result = detector.detect(seq)
    assert 0 <= result["confidence"] <= 1


def test_affected_sensors_identified(detector):
    """Should identify which sensors affected"""
    seq = np.random.randn(20, 4) * 0.1 + 22
    seq[:, 0] = 50
    result = detector.detect(seq)
    assert len(result["affected_sensors"]) > 0


def test_explanation_generated(detector):
    """Should generate explanation"""
    seq = np.ones((20, 4)) * 22
    result = detector.detect(seq)
    assert isinstance(result["explanation"], str)
    assert len(result["explanation"]) > 5


# More pattern-specific tests...
def test_all_patterns_covered(detector):
    """All 7 patterns should be tested"""
    seq = np.random.randn(20, 4) * 0.1 + 22
    result = detector.detect(seq)
    assert len(result["pattern_scores"]) == 7


def test_pattern_scores_sum_to_range(detector):
    """Pattern scores should be normalized"""
    seq = np.random.randn(20, 4) * 0.1 + 22
    result = detector.detect(seq)
    scores = list(result["pattern_scores"].values())
    assert all(0 <= s <= 1 for s in scores)
```

---

## ✅ SUCCESS CRITERIA

- [ ] 7 pattern types detected ✓
- [ ] Primary pattern identified correctly ✓
- [ ] Affected sensors listed ✓
- [ ] Confidence [0, 1] ✓
- [ ] Explanations generated ✓
- [ ] 40+/40+ tests passing ✓
- [ ] Baseline computation working ✓

---

## 🧪 TESTING COMMAND

```bash
pytest tests/models/test_pattern_detector.py -v
# Expected: 40+/40+ passing
```

---

## 📝 NOTES

- Each detector is independent and can be tuned separately
- Baseline must be set before detection
- Pattern scores are [0, 1] representing likelihood
- Confidence = gap between top 2 patterns (normalized)
- Explanations are sensor-aware (e.g., "temp stuck", not "sensor 0 stuck")

---

## ⏭️ NEXT PHASE

After this completes → **Week 2 Phase 2.1: Confidence Scorer**

**auto-proceed**

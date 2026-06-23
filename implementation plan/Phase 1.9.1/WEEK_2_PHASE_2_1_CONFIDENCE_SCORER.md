# WEEK 2 PHASE 2.1: CONFIDENCE SCORER
## Timeline: 1 day | Priority: Assess anomaly legitimacy

---

## 🎯 OBJECTIVE

Build confidence scorer that combines multiple signals to assess **how confident is this anomaly real?**

**Signals Combined:**
1. Reconstruction error strength
2. Pattern clarity (one pattern vs confused)
3. Ensemble agreement (how many detectors agree)
4. Uncertainty (from embedding)

**Output:**
```json
{
  "confidence": 0.92,
  "is_high_confidence": true,
  "is_low_confidence": false
}
```

---

## 📋 DELIVERABLES

- [ ] `src/models/confidence_scorer.py` (200 lines)
- [ ] `tests/models/test_confidence_scorer.py` (15+ tests)
- [ ] Integration with ensemble detector
- [ ] Dashboard: Confidence visualization

---

## 🔧 IMPLEMENTATION

### Create `src/models/confidence_scorer.py`

```python
# src/models/confidence_scorer.py
"""
Confidence Scorer: How confident is this anomaly real?

Combines: reconstruction error + pattern clarity + ensemble agreement + uncertainty
"""

import numpy as np
from typing import Dict
import logging

logger = logging.getLogger(__name__)


class ConfidenceScorer:
    """Score confidence that anomaly is real (not noise)"""
    
    def __init__(self,
                 reconstruction_error_weight: float = 0.4,
                 pattern_clarity_weight: float = 0.3,
                 ensemble_agreement_weight: float = 0.2,
                 uncertainty_weight: float = 0.1):
        """
        Args:
            Weights for combining signals (should sum to 1.0)
        """
        self.weights = {
            "recon_error": reconstruction_error_weight,
            "pattern_clarity": pattern_clarity_weight,
            "ensemble_agreement": ensemble_agreement_weight,
            "uncertainty": uncertainty_weight
        }
        
        # Validate weights sum to 1.0
        total_weight = sum(self.weights.values())
        assert abs(total_weight - 1.0) < 1e-6, f"Weights must sum to 1.0, got {total_weight}"
    
    def score(self,
              reconstruction_error: float,
              pattern_scores: Dict[str, float],
              ensemble_agreement: int,
              uncertainty: float,
              baseline_error: float = 0.05) -> float:
        """
        Compute confidence [0, 1].
        
        High confidence when:
        - High reconstruction error (> baseline)
        - One clear pattern (not confused)
        - Ensemble detectors agree (3-4 out of 4)
        - Low uncertainty (<0.15)
        
        Args:
            reconstruction_error: scalar > 0
            pattern_scores: {"drift": 0.1, "spike": 0.8, ...}
            ensemble_agreement: 1-4 (how many detectors flagged it)
            uncertainty: [0, 1] from embedding uncertainty
            baseline_error: expected error for normal data
        
        Returns:
            confidence: [0, 1]
        """
        
        # Signal 1: Reconstruction error strength
        # Normalize relative to baseline
        error_ratio = reconstruction_error / (baseline_error + 1e-6)
        recon_score = min(error_ratio / 3.0, 1.0)  # 3x baseline = 1.0
        
        # Signal 2: Pattern clarity
        # If one pattern >> others, clarity high
        scores = np.array(list(pattern_scores.values()))
        max_score = scores.max()
        second_score = np.partition(scores, -2)[-2] if len(scores) > 1 else 0
        gap = max_score - second_score
        pattern_clarity = min(gap * 2, 1.0)
        
        # Signal 3: Ensemble agreement
        # 4/4 = 1.0, 3/4 = 0.75, 2/4 = 0.5, 1/4 = 0.25
        ensemble_score = min(ensemble_agreement / 4.0, 1.0)
        
        # Signal 4: Uncertainty (inverse)
        # Low uncertainty = high confidence
        uncertainty_score = 1.0 - min(uncertainty, 1.0)
        
        # Combine signals
        confidence = (
            self.weights["recon_error"] * recon_score +
            self.weights["pattern_clarity"] * pattern_clarity +
            self.weights["ensemble_agreement"] * ensemble_score +
            self.weights["uncertainty"] * uncertainty_score
        )
        
        return float(np.clip(confidence, 0, 1))
    
    def high_confidence(self, confidence: float, threshold: float = 0.75) -> bool:
        """Is this high-confidence anomaly?"""
        return confidence >= threshold
    
    def low_confidence(self, confidence: float, threshold: float = 0.4) -> bool:
        """Is this low-confidence (might be noise)?"""
        return confidence <= threshold
    
    def confidence_level(self, confidence: float) -> str:
        """Get human-readable confidence level"""
        if confidence >= 0.85:
            return "very_high"
        elif confidence >= 0.75:
            return "high"
        elif confidence >= 0.6:
            return "medium"
        elif confidence >= 0.4:
            return "low"
        else:
            return "very_low"
```

---

### Create Tests

```python
# tests/models/test_confidence_scorer.py
import pytest
from src.models.confidence_scorer import ConfidenceScorer


@pytest.fixture
def scorer():
    return ConfidenceScorer()


def test_high_recon_error_increases_confidence(scorer):
    """High reconstruction error should increase confidence"""
    pattern_scores = {
        "drift": 0.1, "spike": 0.1, "dependency": 0.1, "lag": 0.1,
        "saturation": 0.1, "cyclic": 0.1, "cascade": 0.1
    }
    
    low_conf = scorer.score(0.05, pattern_scores, 2, 0.2, baseline_error=0.05)
    high_conf = scorer.score(0.20, pattern_scores, 2, 0.2, baseline_error=0.05)
    
    assert high_conf > low_conf


def test_ensemble_agreement_increases_confidence(scorer):
    """More detectors agreeing should increase confidence"""
    pattern_scores = {
        "drift": 0.8, "spike": 0.1, "dependency": 0.1, "lag": 0.1,
        "saturation": 0.1, "cyclic": 0.1, "cascade": 0.1
    }
    
    low_agreement = scorer.score(0.15, pattern_scores, 1, 0.1, baseline_error=0.05)
    high_agreement = scorer.score(0.15, pattern_scores, 4, 0.1, baseline_error=0.05)
    
    assert high_agreement > low_agreement


def test_clear_pattern_increases_confidence(scorer):
    """Clear pattern (one >> others) should increase confidence"""
    unclear = {
        "drift": 0.3, "spike": 0.3, "dependency": 0.3, "lag": 0.1,
        "saturation": 0.1, "cyclic": 0.1, "cascade": 0.1
    }
    clear = {
        "drift": 0.8, "spike": 0.1, "dependency": 0.05, "lag": 0.05,
        "saturation": 0.05, "cyclic": 0.05, "cascade": 0.05
    }
    
    unclear_conf = scorer.score(0.15, unclear, 3, 0.1)
    clear_conf = scorer.score(0.15, clear, 3, 0.1)
    
    assert clear_conf > unclear_conf


def test_low_uncertainty_increases_confidence(scorer):
    """Low uncertainty should increase confidence"""
    pattern_scores = {
        "drift": 0.8, "spike": 0.1, "dependency": 0.05, "lag": 0.05,
        "saturation": 0.05, "cyclic": 0.05, "cascade": 0.05
    }
    
    high_unc = scorer.score(0.15, pattern_scores, 3, 0.3)
    low_unc = scorer.score(0.15, pattern_scores, 3, 0.1)
    
    assert low_unc > high_unc


def test_confidence_in_range(scorer):
    """Confidence should be [0, 1]"""
    pattern_scores = {
        "drift": 0.5, "spike": 0.3, "dependency": 0.2, "lag": 0.0,
        "saturation": 0.0, "cyclic": 0.0, "cascade": 0.0
    }
    
    conf = scorer.score(0.15, pattern_scores, 3, 0.15)
    assert 0 <= conf <= 1


def test_high_confidence_threshold(scorer):
    """Should correctly classify high confidence"""
    pattern_scores = {
        "drift": 0.8, "spike": 0.05, "dependency": 0.05, "lag": 0.05,
        "saturation": 0.05, "cyclic": 0.0, "cascade": 0.0
    }
    
    conf = scorer.score(0.20, pattern_scores, 4, 0.08)
    assert scorer.high_confidence(conf, threshold=0.75)


def test_low_confidence_threshold(scorer):
    """Should correctly classify low confidence"""
    pattern_scores = {
        "drift": 0.2, "spike": 0.2, "dependency": 0.2, "lag": 0.2,
        "saturation": 0.2, "cyclic": 0.0, "cascade": 0.0
    }
    
    conf = scorer.score(0.05, pattern_scores, 1, 0.5)
    assert scorer.low_confidence(conf, threshold=0.4)


def test_confidence_level_classification(scorer):
    """Should classify confidence levels correctly"""
    assert scorer.confidence_level(0.90) == "very_high"
    assert scorer.confidence_level(0.80) == "high"
    assert scorer.confidence_level(0.65) == "medium"
    assert scorer.confidence_level(0.45) == "low"
    assert scorer.confidence_level(0.20) == "very_low"
```

---

## ✅ SUCCESS CRITERIA

- [ ] Confidence in [0, 1] ✓
- [ ] All 4 signals weighted correctly ✓
- [ ] High error increases confidence ✓
- [ ] Ensemble agreement increases confidence ✓
- [ ] Clear pattern increases confidence ✓
- [ ] Low uncertainty increases confidence ✓
- [ ] 10/10 tests passing ✓

---

## 🧪 TESTING COMMAND

```bash
pytest tests/models/test_confidence_scorer.py -v
# Expected: 10/10 passing
```

---

## 📝 NOTES

- Weights: 40% error, 30% clarity, 20% agreement, 10% uncertainty
- Confidence thresholds: 0.75 (high), 0.4 (low)
- Used for filtering false positives
- Can be tuned per customer based on feedback

---

## ⏭️ NEXT PHASE

After this completes → **Week 2 Phase 2.2: Root Cause + Recommendations**

**auto-proceed**

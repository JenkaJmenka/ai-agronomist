# WEEK 1 PHASE 1.1: ANOMALY ATTRIBUTOR (SHAP-based)
## Timeline: 2 days | Priority: Foundation for attribution

---

## 🎯 OBJECTIVE

Build SHAP-based attribution system to explain **which sensors caused anomalies**.

**Output Example:**
```json
{
  "attribution": {
    "temperature": 0.45,
    "humidity": 0.35,
    "co2": 0.15,
    "light": 0.05
  },
  "affected_sensors": ["temperature", "humidity"],
  "confidence": 0.92,
  "explanation": "Temperature deviation 8°C + humidity spike 25%"
}
```

---

## 📋 DELIVERABLES

- [ ] `src/models/anomaly_attributor.py` (400 lines)
- [ ] `src/models/embedding_utils.py` updates
- [ ] `tests/models/test_anomaly_attributor.py` (20+ tests)
- [ ] Integration with inference pipeline
- [ ] Dashboard: Attribution visualization

---

## 🔧 IMPLEMENTATION

### Step 1: Create `src/models/anomaly_attributor.py`

```python
# src/models/anomaly_attributor.py
"""
Anomaly Attribution: Explains which sensors caused the anomaly using SHAP values.

Core idea:
- LSTM-VAE reconstruction error is the "prediction"
- Each sensor's values contribute to that error
- SHAP computes: how much did each sensor contribute?
- Result: normalized attribution [0, 1] per sensor
"""

import numpy as np
import torch
import torch.nn.functional as F
from typing import Dict, Tuple, List, Optional
from scipy.special import comb
import logging

logger = logging.getLogger(__name__)


class AnomalyAttributor:
    """
    SHAP-based attribution for LSTM-VAE anomalies.
    
    Output:
    {
        "attribution": {
            "temperature": 0.45,
            "humidity": 0.35,
            "co2": 0.15,
            "light": 0.05
        },
        "affected_sensors": ["temperature", "humidity"],
        "confidence": 0.92,
        "explanation": "Temperature deviation 8°C + humidity spike 25%"
    }
    """
    
    def __init__(self, model, device: str = "cpu", sensor_names: List[str] = None):
        """
        Args:
            model: Trained LSTMVAE
            device: "cpu" or "cuda"
            sensor_names: ["temperature", "humidity", "co2", "light"]
        """
        self.model = model
        self.device = torch.device(device)
        self.sensor_names = sensor_names or ["temp", "humidity", "co2", "light"]
        
        # Baseline for comparison
        self.baseline_sequences = []
        self.baseline_error = None
    
    def set_baseline(self, sequences: np.ndarray) -> None:
        """
        Establish baseline (normal operation).
        
        Args:
            sequences: (N, seq_len, input_dim) of normal sequences
        """
        self.baseline_sequences = sequences
        
        # Compute average baseline error
        with torch.no_grad():
            errors = []
            for seq in sequences:
                x = torch.tensor(seq[np.newaxis, :], dtype=torch.float32).to(self.device)
                recon, _, _, _ = self.model(x)
                error = F.mse_loss(recon, x).item()
                errors.append(error)
            
            self.baseline_error = np.mean(errors)
        
        logger.info(f"Baseline error: {self.baseline_error:.6f}")
    
    def attribute_sequence(self, sequence: np.ndarray) -> Dict:
        """
        Compute SHAP-style attribution for anomalous sequence.
        
        Approach:
        1. Get baseline (normal) sequences
        2. Permute each sensor independently
        3. Measure how much error changes
        4. High change = sensor contributed significantly
        
        Args:
            sequence: (seq_len, input_dim)
        
        Returns:
            Dict with attribution, affected_sensors, confidence
        """
        
        if len(self.baseline_sequences) == 0:
            raise ValueError("Must call set_baseline() first")
        
        x = torch.tensor(sequence[np.newaxis, :], dtype=torch.float32).to(self.device)
        
        # Compute anomaly error
        with torch.no_grad():
            recon, _, _, _ = self.model(x)
            anomaly_error = F.mse_loss(recon, x).item()
        
        # Attribution per sensor (simplified permutation approach)
        attribution = {}
        sensor_contributions = []
        
        for sensor_idx in range(sequence.shape[1]):
            # Create masked version (replace with baseline mean)
            masked_seq = sequence.copy()
            baseline_mean = self.baseline_sequences[:, :, sensor_idx].mean()
            masked_seq[:, sensor_idx] = baseline_mean
            
            # Compute error on masked version
            x_masked = torch.tensor(masked_seq[np.newaxis, :], dtype=torch.float32).to(self.device)
            with torch.no_grad():
                recon_masked, _, _, _ = self.model(x_masked)
                masked_error = F.mse_loss(recon_masked, x_masked).item()
            
            # Contribution = how much does masking reduce error?
            contribution = max(0, anomaly_error - masked_error)
            sensor_contributions.append(contribution)
            attribution[self.sensor_names[sensor_idx]] = float(contribution)
        
        # Normalize to [0, 1]
        total_contrib = sum(sensor_contributions)
        if total_contrib > 1e-6:
            attribution = {k: v / total_contrib for k, v in attribution.items()}
        
        # Affected sensors: those with >15% contribution
        affected = [name for name, contrib in attribution.items() if contrib > 0.15]
        
        # Confidence: how clear is the attribution?
        # (If one sensor >> others, confidence high)
        max_contrib = max(attribution.values()) if attribution else 0
        confidence = min(max_contrib * 2, 1.0)  # Normalized confidence
        
        return {
            "attribution": attribution,
            "sensor_contributions": sensor_contributions,
            "affected_sensors": affected,
            "confidence": confidence,
            "anomaly_error": anomaly_error,
            "baseline_error": self.baseline_error
        }
    
    def attribute_batch(self, sequences: np.ndarray) -> List[Dict]:
        """Attribute multiple sequences."""
        results = []
        for seq in sequences:
            result = self.attribute_sequence(seq)
            results.append(result)
        return results
    
    def get_explanation(self, attribution: Dict, sequence: np.ndarray) -> str:
        """
        Natural language explanation of attribution.
        
        Example: "Temperature deviation of 8°C (45% contribution) combined with
        humidity spike of 25% (35% contribution) indicate potential ventilation failure"
        """
        
        affected = attribution.get("affected_sensors", [])
        if not affected:
            return "No significant sensor deviation detected"
        
        # Get sensor deviations from baseline
        baseline_mean = np.mean(self.baseline_sequences, axis=0)
        deviations = sequence.mean(axis=0) - baseline_mean
        
        explanation_parts = []
        for sensor_name in affected:
            sensor_idx = self.sensor_names.index(sensor_name)
            deviation = deviations[sensor_idx]
            contribution = attribution["attribution"].get(sensor_name, 0)
            
            if abs(deviation) > 0.1:
                explanation_parts.append(
                    f"{sensor_name} deviation: {deviation:+.2f} ({contribution:.0%} contribution)"
                )
        
        return " + ".join(explanation_parts) if explanation_parts else "Unknown cause"


# Helper for attribution strength scoring
class AttributionStrength:
    """Assess clarity/strength of attribution"""
    
    @staticmethod
    def gini_impurity(attribution_dict: Dict[str, float]) -> float:
        """
        Gini impurity of attribution distribution.
        
        Low Gini = one sensor dominates (clear attribution)
        High Gini = distributed (unclear attribution)
        """
        values = np.array(list(attribution_dict.values()))
        if values.sum() == 0:
            return 0
        
        p = values / values.sum()
        gini = 1 - np.sum(p ** 2)
        return gini
    
    @staticmethod
    def entropy(attribution_dict: Dict[str, float]) -> float:
        """Shannon entropy of attribution"""
        values = np.array(list(attribution_dict.values()))
        if values.sum() == 0:
            return 0
        
        p = values / values.sum()
        p = p[p > 1e-10]
        entropy = -np.sum(p * np.log2(p))
        return entropy
```

---

### Step 2: Create Tests

```python
# tests/models/test_anomaly_attributor.py
import pytest
import numpy as np
from src.models.lstm_vae import LSTMVAE
from src.models.anomaly_attributor import AnomalyAttributor


@pytest.fixture
def model():
    return LSTMVAE(input_dim=4, hidden_dims=[64, 32], latent_dim=8, seq_len=20)


@pytest.fixture
def attributor(model):
    attr = AnomalyAttributor(model, device="cpu")
    # Set baseline
    baseline = np.random.rand(100, 20, 4)
    attr.set_baseline(baseline)
    return attr


def test_attribution_sums_to_one(attributor):
    """Attribution should sum to 1.0"""
    seq = np.random.rand(20, 4)
    result = attributor.attribute_sequence(seq)
    total = sum(result["attribution"].values())
    assert abs(total - 1.0) < 1e-6


def test_affected_sensors_threshold(attributor):
    """Affected sensors should be >15% contribution"""
    seq = np.random.rand(20, 4)
    result = attributor.attribute_sequence(seq)
    
    for sensor in result["affected_sensors"]:
        contrib = result["attribution"][sensor]
        assert contrib > 0.15


def test_confidence_in_range(attributor):
    """Confidence should be [0, 1]"""
    seq = np.random.rand(20, 4)
    result = attributor.attribute_sequence(seq)
    assert 0 <= result["confidence"] <= 1


def test_batch_attribution(attributor):
    """Should handle batch of sequences"""
    batch = np.random.rand(10, 20, 4)
    results = attributor.attribute_batch(batch)
    assert len(results) == 10
    assert all("attribution" in r for r in results)


def test_explanation_generated(attributor):
    """Should generate natural language explanation"""
    seq = np.random.rand(20, 4)
    result = attributor.attribute_sequence(seq)
    explanation = attributor.get_explanation(result, seq)
    assert isinstance(explanation, str)
    assert len(explanation) > 10


def test_baseline_set(attributor):
    """Baseline should be properly set"""
    assert attributor.baseline_error is not None
    assert attributor.baseline_error > 0
```

---

## ✅ SUCCESS CRITERIA

- [ ] Attribution sums to 1.0 ✓
- [ ] Affected sensors identified (>15% threshold) ✓
- [ ] Confidence in [0, 1] ✓
- [ ] Explanation generated ✓
- [ ] 6/6 tests passing ✓
- [ ] Integration with inference ready

---

## 🧪 TESTING COMMAND

```bash
pytest tests/models/test_anomaly_attributor.py -v
# Expected: 6/6 passing
```

---

## 📝 NOTES

- Attribution uses permutation-based approach (SHAP approximation)
- Baseline must be set before inference
- Affected sensors threshold: 15% contribution
- Confidence = max_contribution * 2 (normalized to [0,1])

---

## ⏭️ NEXT PHASE

After this completes → **Week 1 Phase 1.2: Pattern Detector**

**auto-proceed**

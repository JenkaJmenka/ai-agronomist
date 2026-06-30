# Project: Two-Pass Anomaly Detection System - Phase 2

## Architecture
This phase implements univariate anomaly detectors, a voting engine, active learning components, a threshold optimizer, and a feedback API.

```
                  [Operator Feedback API]
                             │
                      (Writes to DB)
                             ▼
                 [database.models.OperatorFeedback]
                             │
                             ▼
                 [OperatorFeedbackLearner] (active_learner)
                             │
                             ├─ (triggers retrain at 20+ feedback samples)
                             ▼
                 [ThresholdOptimizer] (grid search over parameters)
                             │
                             ▼
             [Univariate Detectors & Voting Engine]
 (Spike, Stuck, Drift, Precision, Polling -> Weighted Voting Consensus)
```

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| 1 | Univariate Detectors & Voting Engine | `detectors/base.py`, `detectors/spike.py`, `detectors/stuck.py`, `detectors/drift.py`, `detectors/precision.py`, `detectors/polling.py`, `engines/voting.py` | None | DONE |
| 2 | Active Learner & Threshold Optimizer | `models/active_learner.py`, `engines/threshold_optimizer.py` | Milestone 1 | IN_PROGRESS (cded3b32) |
| 3 | Feedback API & Database Integration | `api/routes/feedback.py` | Milestone 2 | PLANNED |
| 4 | Unit Testing & Validation | `tests/unit/test_detectors.py`, `tests/unit/test_voting.py`, `tests/unit/test_active_learner.py` | Milestone 3 | PLANNED |

## Interface Contracts
### BaseDetector
```python
class DetectionResult:
    triggered: bool
    confidence: float
    details: str

class BaseDetector(ABC):
    def check(self, value: float, pattern: dict, sensor: str) -> DetectionResult
```

### Voting Engine
```python
def aggregate_detector_votes(detectors_triggered: List[Dict]) -> float
def get_consensus_level(detector_count: int) -> str
```

### OperatorFeedbackLearner
```python
class OperatorFeedbackLearner:
    def record_feedback(self, job_id: str, timestamp: str, sensor: str, is_true_anomaly: bool, operator_id: str, reason: str = None) -> Dict
```

### ThresholdOptimizer
```python
class ThresholdOptimizer:
    def optimize(feedback_history: List[Dict], detectors: List, pattern: Dict) -> Dict[str, float]
```

### Feedback API
- `POST /api/feedback/mark-anomaly`
  Inputs: `job_id`, `timestamp`, `sensor`, `is_true_anomaly`, `operator_id`, `customer_id`, `reason` (optional)
  Returns: feedback status, counts, or retraining trigger confirmation.
- `GET /api/feedback/statistics`
  Inputs: `customer_id`
  Returns: statistics on feedback count and operator accuracy metrics.

## Code Layout
- `detectors/base.py`
- `detectors/spike.py`
- `detectors/stuck.py`
- `detectors/drift.py`
- `detectors/precision.py`
- `detectors/polling.py`
- `engines/voting.py`
- `engines/threshold_optimizer.py`
- `models/active_learner.py`
- `api/routes/feedback.py`
- `tests/unit/test_detectors.py`
- `tests/unit/test_voting.py`
- `tests/unit/test_active_learner.py`

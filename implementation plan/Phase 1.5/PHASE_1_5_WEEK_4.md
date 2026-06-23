# Phase 1.5 - WEEK 4: Feedback Loop & Auto-Tuning
## Operator Feedback + Threshold Optimization (Sequential, 20 min)

---

## 📋 **Week 4 Overview**

**Sequential** — runs AFTER Week 3 completes  
**Duration:** ~20 minutes  
**Goal:** Implement operator feedback collection and threshold auto-tuning

**Dependencies:**
- ✅ Week 3 complete (Seasonal models working)

---

## 🚀 **Submit This to Orchestrator**

Wait for Week 3 to complete, then paste:

---

## **ORCHESTRATOR SUBMISSION TEXT**

```
Continue Phase 1.5 Week 4 - Operator feedback loop and auto-threshold tuning.

Phase 1.5.4 (after Phase 1.5.3):

- backend-engineer: Implement feedback collection UI and threshold optimizer.
  Output: src/models/feedback_aggregator.py, src/models/threshold_optimizer.py, src/api/routes/feedback.py
  
  Deliverables:
  
  1. src/models/feedback_aggregator.py
     class FeedbackAggregator:
       
       async def record_feedback(self,
                                customer_id: str,
                                sequence_id: str,
                                is_false_positive: bool,
                                feedback_type: str = "manual"):
         """
         Record operator feedback.
         
         Args:
         - customer_id: Which customer
         - sequence_id: Which sequence was flagged
         - is_false_positive: True if operator says it's not actually anomalous
         - feedback_type: "manual" or "auto" (for future)
         
         Stores in database: operator_feedback table
         """
       
       async def get_feedback_stats(self, customer_id: str) -> Dict:
         """
         Get feedback statistics for customer.
         
         Returns:
         {
           "total_feedback": 45,
           "false_positives": 12,
           "false_positive_rate": 0.27,
           "recent_feedback": [
             {"sequence_id": "...", "was_anomaly": false, "confidence": 0.95},
             {...}
           ]
         }
         """
       
       async def get_threshold_recommendations(self,
                                             customer_id: str) -> Dict:
         """
         Analyze feedback, recommend new threshold.
         
         Logic:
         - If false_positive_rate > 20%, recommend LOWER threshold
         - If false_negative_rate > 10%, recommend RAISE threshold
         - Calculate: which threshold minimizes error?
         
         Returns:
         {
           "current_threshold": 0.5,
           "recommended_threshold": 0.45,
           "reason": "27% false positives, recommend lower",
           "confidence": 0.85
         }
         """
  
  2. src/models/threshold_optimizer.py
     class ThresholdOptimizer:
       
       async def optimize_threshold(self,
                                   customer_id: str,
                                   feedback_data: List[Dict]) -> float:
         """
         Calculate optimal threshold based on feedback.
         
         Algorithm:
         1. Get all feedback entries for customer
         2. For each feedback entry:
            - Sequence ID → model inference score
            - Compare score to actual (false positive yes/no)
         3. Calculate best threshold that minimizes:
            - False positives + 2*False negatives (weight negatives higher)
         4. Return optimal threshold
         """
       
       def _find_optimal_threshold(self,
                                  scores: List[float],
                                  labels: List[bool]) -> float:
         """
         Find threshold that minimizes error.
         
         Try all possible thresholds (0.0 to 1.0, step 0.01)
         For each threshold:
         - Count false positives
         - Count false negatives
         - Cost = FP + 2*FN
         Return threshold with lowest cost
         """
       
       async def apply_threshold(self,
                                customer_id: str,
                                new_threshold: float) -> bool:
         """
         Update threshold in model registry.
         
         Store in: model_version.threshold field
         """
  
  3. src/api/routes/feedback.py
     New endpoints:
     
     - POST /api/feedback
       Record feedback for anomaly
       Request: {
         "customer_id": "...",
         "sequence_id": "...",
         "is_false_positive": true,
         "notes": "This is normal for us"
       }
       Response: {"status": "recorded"}
     
     - GET /api/feedback/stats/{customer_id}
       Get feedback statistics
       Response: {
         "total_feedback": 45,
         "false_positive_rate": 0.27,
         "recommended_threshold": 0.45
       }
     
     - POST /api/feedback/optimize-threshold
       Trigger threshold optimization
       Request: {"customer_id": "..."}
       Response: {
         "new_threshold": 0.45,
         "previous_threshold": 0.5,
         "reason": "27% false positives, lowered threshold"
       }

- backend-engineer: Update dashboard with feedback UI.
  Output: src/dashboard/pages/mark_anomalies.py
  
  Deliverable:
  
  New Streamlit page: src/dashboard/pages/mark_anomalies.py
  
  Functionality:
  
  1. Display flagged sequences (recent anomalies)
     - Table: sequence_id | timestamp | anomaly_score | sensors
     - Show only sequences with anomaly_score > threshold
     - List most recent first
  
  2. For each sequence:
     - Charts: show the sensor readings (why was it flagged?)
     - Operator marks: "False positive" or "Correct alert"
     - Notes: operator can add context
     - Save button: stores feedback
  
  3. Feedback summary:
     - "You've marked 45 anomalies"
     - "False positive rate: 27%"
     - "Recommended threshold: 0.45 (lower sensitivity)"
  
  4. Auto-threshold button:
     - Button: "Optimize Threshold"
     - Shows: current → recommended
     - Operator can apply (or ignore)
  
  UI Flow:
  ```
  Top: "Recent Anomalies" section
       [Show 10 most recent flagged sequences]
  
  For each sequence:
    - Line chart: 4 sensors over 20 timesteps
    - Text: "Anomaly Score: 0.85"
    - Button: "This is NORMAL for us" [mark as false positive]
    - Button: "This IS an anomaly" [confirm]
  
  Bottom: "Feedback Summary"
    - Stats: total feedback, false positive rate
    - Recommendation: current threshold vs recommended
    - Button: "Apply recommended threshold"
  ```

- qa-engineer: Test feedback loop and threshold optimization.
  Output: tests/models/test_feedback_aggregator.py, tests/models/test_threshold_optimizer.py, tests/api/test_feedback.py
  
  Deliverables:
  
  1. tests/models/test_feedback_aggregator.py
     Test cases:
     - test_record_feedback: Feedback saved to database
     - test_get_feedback_stats: Statistics calculated correctly
     - test_false_positive_rate: Correct calculation (12/45 = 27%)
     - test_get_threshold_recommendations: Recommends lower if high FP rate
     - test_aggregation_per_customer: Customer A feedback ≠ Customer B
  
  2. tests/models/test_threshold_optimizer.py
     Test cases:
     - test_find_optimal_threshold: Minimizes errors
     - test_threshold_improves_fp_rate: New threshold reduces false positives
     - test_threshold_stored: Updated threshold saved to model registry
     - test_prevent_threshold_flip_flop: Changes are gradual (not jumping 0.5→0.1)
  
  3. tests/api/test_feedback.py
     Test cases:
     - test_post_feedback: POST /api/feedback records feedback
     - test_get_feedback_stats: GET /api/feedback/stats returns stats
     - test_optimize_threshold: POST /api/feedback/optimize-threshold updates
     - test_feedback_isolation: Customer A can't see Customer B feedback
  
  Integration test fixture:
  - Simulate 45 feedback entries
  - Some false positives (12), some correct (33)
  - Verify threshold optimizer recommends lower threshold

---

## ✅ **Week 4 Success Criteria**

- [ ] `src/models/feedback_aggregator.py` records and aggregates feedback
- [ ] `src/models/threshold_optimizer.py` calculates optimal threshold
- [ ] `src/api/routes/feedback.py` implements 3 endpoints
- [ ] Dashboard page `mark_anomalies.py` shows anomalies for marking
- [ ] Operator can mark false positives in dashboard
- [ ] Feedback stats show: total feedback, false positive rate
- [ ] Threshold optimizer recommends new threshold
- [ ] Threshold auto-tuning reduces false positives
- [ ] `tests/models/test_feedback_aggregator.py` passes (5 tests)
- [ ] `tests/models/test_threshold_optimizer.py` passes (4 tests)
- [ ] `tests/api/test_feedback.py` passes (4 tests)

---

## 🧪 **Test Commands** (After Week 4)

```bash
# Test feedback aggregator
pytest tests/models/test_feedback_aggregator.py -v

# Test threshold optimizer
pytest tests/models/test_threshold_optimizer.py -v

# Test feedback API
pytest tests/api/test_feedback.py -v

# Check feedback stored in database
sqlite3 app.db "SELECT COUNT(*) FROM operator_feedback;"
# Should show: 45 (from test)
```

---

## 📊 **Feedback Flow Example**

```
Day 1: Operator notices high anomaly alerts
  - Charts on dashboard show alerts, but greenhouse looks normal
  - Operator marks 5 sequences as "False positive"
  
Day 2: Aggregator analyzes
  - 45 total anomalies marked
  - 12 marked as false positive (27% false positive rate)
  - Recommends: lower threshold from 0.50 → 0.45
  
Day 3: Operator applies recommendation
  - New threshold: 0.45
  - Now only detects 35 anomalies (8 fewer false positives)
  
Day 4: Model improves
  - Fewer alerts, but still catches real anomalies
  - False positive rate down to 15%
```

---

## 💡 **Key Design Points**

- **Feedback Collection:** Mark sequences as false positives
- **Aggregation:** Calculate false positive rate per customer
- **Threshold Optimization:** Use feedback to find best threshold
- **Gradual Adjustment:** Don't flip thresholds drastically
- **Per-Customer:** Each customer's feedback is independent

---

## 📍 **What Happens Next**

When Week 4 completes:
1. ✅ Operator feedback working
2. ✅ False positives marked in dashboard
3. ✅ Threshold auto-tuned based on feedback
4. ✅ Fewer false positives, better accuracy

Next: **Week 5** (A/B testing - compare models)

---

**Ready?** Wait for Week 3 to finish! 🚀

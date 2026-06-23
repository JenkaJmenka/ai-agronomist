# Phase 1.5 - WEEK 5: A/B Testing & Performance
## Model Comparison & Performance Monitoring (Sequential, 20 min)

---

## 📋 **Week 5 Overview**

**Sequential** — runs AFTER Week 4 completes  
**Duration:** ~20 minutes  
**Goal:** Implement A/B testing framework and performance monitoring

**Dependencies:**
- ✅ Week 4 complete (Feedback loop working)

---

## 🚀 **Submit This to Orchestrator**

Wait for Week 4 to complete, then paste:

---

## **ORCHESTRATOR SUBMISSION TEXT**

```
Continue Phase 1.5 Week 5 - A/B testing framework and performance monitoring.

Phase 1.5.5 (after Phase 1.5.4):

- architect: Design A/B testing and performance monitoring strategy.
  Output: docs/architecture/ab_testing_design.md
  
  Deliverable:
  Design document covering:
  
  1. A/B Testing Strategy
     - What: Compare old model (A) vs new model (B)
     - Duration: 1-2 weeks (collect inference data)
     - Metrics: accuracy, latency, false positive rate
     - Decision: which model wins?
     - Rollback: can revert to A if B is worse
  
  2. Traffic Split
     - Option 1: 50/50 split (half sequences use A, half use B)
     - Option 2: 10/90 split (10% on B for safety)
     - Recommendation: Start 10/90, increase to 50/50 after 3 days
  
  3. Performance Metrics
     - Inference latency (milliseconds)
     - Anomaly detection accuracy (vs ground truth)
     - False positive rate (from feedback)
     - Model size (memory footprint)
     - Training time (how long to produce model)
  
  4. Statistical Significance
     - Need enough samples (1000+) before deciding
     - Confidence level: 95%
     - Can model A truly beat model B?

- ml-engineer: Implement A/B testing framework.
  Output: src/models/ab_tester.py, src/api/routes/ab_tests.py
  
  Deliverables:
  
  1. src/models/ab_tester.py
     class ABTester:
       
       async def create_test(self,
                            customer_id: str,
                            model_a_id: str,
                            model_b_id: str,
                            split_ratio: float = 0.1) -> str:
         """
         Create A/B test.
         
         Args:
         - split_ratio: 0.1 = 10% on B, 90% on A
         
         Returns: test_id (unique identifier)
         
         Stores in database: ab_test table
         """
       
       def decide_model(self, test_id: str) -> str:
         """
         Given test_id, randomly decide A or B for this inference.
         
         Uses split_ratio to determine probability
         Returns: "model_a_id" or "model_b_id"
         """
       
       async def record_inference(self,
                                 test_id: str,
                                 model_used: str,
                                 latency_ms: int,
                                 anomaly_score: float,
                                 feedback_label: Optional[bool] = None):
         """
         Record inference in A/B test.
         
         Stores: which model, latency, score, feedback (if available)
         """
       
       async def get_test_results(self, test_id: str) -> Dict:
         """
         Get A/B test results.
         
         Returns:
         {
           "test_id": "...",
           "status": "running" or "completed",
           "model_a": {
             "inferences": 1000,
             "avg_latency_ms": 45,
             "false_positive_rate": 0.25
           },
           "model_b": {
             "inferences": 110,
             "avg_latency_ms": 42,
             "false_positive_rate": 0.20
           },
           "winner": "model_b" (or "tie" or "no_decision_yet"),
           "confidence": 0.92
         }
         """
       
       async def is_statistically_significant(self,
                                            model_a_results: Dict,
                                            model_b_results: Dict) -> Tuple[bool, float]:
         """
         Check if difference is statistically significant (95% confidence).
         
         Returns: (is_significant, confidence_level)
         """

  2. src/api/routes/ab_tests.py
     New endpoints:
     
     - POST /api/ab-tests
       Create A/B test
       Request: {
         "customer_id": "...",
         "model_a_id": "...",
         "model_b_id": "...",
         "split_ratio": 0.1
       }
       Response: {"test_id": "...", "status": "created"}
     
     - GET /api/ab-tests/{test_id}
       Get test results
       Response: {
         "test_id": "...",
         "status": "running",
         "model_a": {...},
         "model_b": {...},
         "winner": null
       }
     
     - POST /api/ab-tests/{test_id}/finish
       Finish test, declare winner
       Response: {
         "winner": "model_b",
         "reason": "22% lower false positive rate",
         "recommendation": "deploy model_b"
       }

- backend-engineer: Integrate A/B testing into inference.
  Output: src/api/routes/inference.py (updated), src/models/inference.py (updated)
  
  Deliverables:
  
  1. src/api/routes/inference.py (UPDATED)
     Modify POST /api/inference:
     - New optional field: "ab_test_id"
     - If ab_test_id provided:
       * ABTester decides which model to use
       * Record inference for A/B test
       * Track latency
     
     Example request:
     {
       "customer_id": "customer_tomato_1",
       "sequences": [...],
       "ab_test_id": "test_v3_vs_v4"
     }
     
     Response includes:
     {
       "model_used": "model_b",
       "anomaly_scores": [...],
       "latency_ms": 42,
       "ab_test_id": "test_v3_vs_v4"
     }
  
  2. Inference timing:
     - Measure latency: time from request to response
     - Record in A/B test
     - Display: average latency per model in A/B test results

- backend-engineer: Update dashboard with A/B test results.
  Output: src/dashboard/pages/model_comparison.py
  
  Deliverable:
  
  New Streamlit page: src/dashboard/pages/model_comparison.py
  
  Functionality:
  
  1. List active A/B tests
     - Test: Model A vs Model B
     - Duration: "Running for 2 days"
     - Traffic split: "10% on B, 90% on A"
     - Status: "Need more data" or "B is better" or "No winner yet"
  
  2. Detailed results
     Comparison table:
     | Metric | Model A | Model B | Winner |
     |--------|---------|---------|--------|
     | Inferences | 1000 | 110 | - |
     | Avg Latency | 45ms | 42ms | B |
     | False Positive Rate | 25% | 20% | B |
     | Recall | 85% | 87% | B |
     | Confidence | - | - | 92% |
  
  3. Decision buttons
     - "Deploy Model B" (if B wins)
     - "Extend test" (if tie or need more data)
     - "Rollback to Model A" (if B worse)
  
  4. Progress chart
     - X-axis: days
     - Y-axis: metric value
     - Show how metrics change over time

- qa-engineer: Test A/B testing framework.
  Output: tests/models/test_ab_tester.py, tests/api/test_ab_tests.py, tests/integration/test_ab_test_end_to_end.py
  
  Deliverables:
  
  1. tests/models/test_ab_tester.py
     Test cases:
     - test_create_ab_test: Test created in database
     - test_decide_model: 10% ratio → ~10% choose B
     - test_record_inference: Inference recorded in test
     - test_get_test_results: Results aggregated correctly
     - test_statistical_significance: Can detect winner
     - test_result_latency: Latency recorded per model
  
  2. tests/api/test_ab_tests.py
     Test cases:
     - test_create_ab_test_endpoint: POST /api/ab-tests
     - test_get_ab_test_results: GET /api/ab-tests/{test_id}
     - test_finish_ab_test: POST /api/ab-tests/{test_id}/finish
     - test_inference_with_ab_test: Inference records to test
  
  3. tests/integration/test_ab_test_end_to_end.py
     Full workflow:
     - Create A/B test (Model A vs B)
     - Run 1000 inferences (split 10/90)
     - Simulate feedback (B has lower FP rate)
     - Declare winner (B)
     - Deploy B
     - Verify new inferences use B

---

## ✅ **Week 5 Success Criteria**

- [ ] `src/models/ab_tester.py` implements A/B testing
- [ ] `src/api/routes/ab_tests.py` implements 3 endpoints
- [ ] Can create A/B test comparing two models
- [ ] Can run inferences in A/B test (split traffic)
- [ ] Results show winner with confidence level
- [ ] Inference latency tracked per model
- [ ] Dashboard shows A/B test results
- [ ] Can declare winner and deploy
- [ ] Can rollback if needed
- [ ] `tests/models/test_ab_tester.py` passes (6 tests)
- [ ] `tests/api/test_ab_tests.py` passes (4 tests)
- [ ] `tests/integration/test_ab_test_end_to_end.py` passes (1 test)

---

## 🧪 **Test Commands** (After Week 5)

```bash
# Test A/B testing
pytest tests/models/test_ab_tester.py -v

# Test A/B API
pytest tests/api/test_ab_tests.py -v

# Test end-to-end
pytest tests/integration/test_ab_test_end_to_end.py -v -s

# Check A/B test in database
sqlite3 app.db "SELECT test_id, status FROM ab_test LIMIT 5;"
```

---

## 📊 **A/B Test Example**

```
Scenario: Customer Tomato comparing two models

Day 1-3: Traffic 10% on Model B, 90% on Model A
  - Collect safety data
  - B getting enough samples
  
Day 4-7: Traffic 50% on B, 50% on A
  - More data on B
  - Statistical significance increases
  
Results after 1 week:
  Model A: latency=45ms, FP_rate=25%
  Model B: latency=42ms, FP_rate=20%
  
  Confidence: 94% (B is truly better)
  
Decision: Deploy Model B ✅
```

---

## 💡 **Key Design Points**

- **Traffic Split:** Start conservative (10/90), increase if safe
- **Statistical Significance:** Need 95% confidence before declaring winner
- **Latency Tracking:** Important for user experience
- **Reversible:** Can always rollback to Model A
- **Per-Customer:** Each customer can run their own A/B tests

---

## 📍 **What Happens Next**

When Week 5 completes:
1. ✅ A/B testing framework working
2. ✅ Can safely test new models
3. ✅ Latency and accuracy metrics tracked
4. ✅ Clear winner declared (or rollback)

Next: **Week 6** (Priva integration + production hardening)

---

**Ready?** Wait for Week 4 to finish! 🚀

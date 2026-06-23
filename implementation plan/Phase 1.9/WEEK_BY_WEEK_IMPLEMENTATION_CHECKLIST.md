# PHASE 1.5 IMPLEMENTATION CHECKLIST
## Week-by-Week Execution Guide for AI Agronomist

**Print this out! Track progress daily.**

---

## 🗓️ WEEK 1: EMBEDDINGS + INCIDENT MEMORY

### Gap 1: Vector Embeddings (2 days)

**Monday-Tuesday:**
- [ ] Create `src/models/anomaly_embedding.py`
  - [ ] `AnomalyEmbedding` class
  - [ ] `embed_sequence()` method
  - [ ] `embed_batch()` method
  - [ ] `embed_with_uncertainty()` method
  - [ ] `embedding_distance()` method
  - [ ] `embedding_similarity()` method
  - [ ] `cluster_embeddings()` method
  - [ ] `decode_embedding()` method

- [ ] Create `src/models/embedding_utils.py`
  - [ ] `normalize_embeddings()`
  - [ ] `embedding_statistics()`
  - [ ] `find_nearest_embeddings()`
  - [ ] `embedding_projection_2d()`

- [ ] Update `src/models/inference.py`
  - [ ] Import `AnomalyEmbedding`
  - [ ] Create `AnomalyDetectorWithEmbeddings` class
  - [ ] Add `predict()` method (returns embedding)
  - [ ] Add `predict_batch()` method

- [ ] Update `src/api/schemas/sensor_data.py`
  - [ ] Add `embeddings: List[List[float]]` to response
  - [ ] Add `uncertainties: List[float]`
  - [ ] Add `embedding_dim: int = 8`

- [ ] Create tests: `tests/models/test_anomaly_embedding.py`
  - [ ] `test_embed_sequence_shape` ✓
  - [ ] `test_embed_batch_shape` ✓
  - [ ] `test_embed_with_uncertainty` ✓
  - [ ] `test_embedding_distance` ✓
  - [ ] `test_embedding_similarity` ✓
  - [ ] `test_cluster_embeddings` ✓
  - [ ] `test_decode_embedding` ✓
  - Run: `pytest tests/models/test_anomaly_embedding.py -v`
  - [ ] All tests passing ✓

**Wednesday:**
- [ ] Integration testing
  - [ ] Inference API returns embeddings ✓
  - [ ] Embeddings are 8-dimensional ✓
  - [ ] Uncertainty values reasonable (0-1) ✓
  - [ ] Performance: <5ms overhead ✓

- [ ] Dashboard component
  - [ ] Add embedding visualization widget
  - [ ] Show: first embedding, latest embedding
  - [ ] Show: embedding statistics

**Thursday:**
- [ ] Code review
  - [ ] Check: Code style, docstrings
  - [ ] Check: Error handling
  - [ ] Check: Performance

- [ ] Staging deployment
  - [ ] Deploy to staging environment
  - [ ] Run full test suite
  - [ ] Verify embeddings in API responses

**Friday:**
- [ ] Documentation
  - [ ] Write: `docs/embeddings_architecture.md`
  - [ ] API examples with embedding fields
  - [ ] Troubleshooting guide

---

### Gap 2: Incident Memory with pgvector (2 days)

**Monday-Tuesday (PARALLEL with Gap 1):**

- [ ] PostgreSQL + pgvector setup
  - [ ] Provision PostgreSQL instance (RDS or local)
  - [ ] Install pgvector extension: `CREATE EXTENSION vector`
  - [ ] Test vector type: `SELECT '(1,2,3)'::vector`

- [ ] Update database models: `src/db/models.py`
  - [ ] Add `Incident` table with:
    - [ ] id, customer_id
    - [ ] detected_at, resolved_at, reported_at
    - [ ] sequence (JSONB), anomaly_score
    - [ ] embedding (Vector(8)) ← CRITICAL
    - [ ] embedding_uncertainty
    - [ ] root_cause, root_cause_confidence
    - [ ] resolved, actions_taken, actions_effective
    - [ ] time_to_detect, time_to_resolve
    - [ ] tags, notes
  - [ ] Add `IncidentAction` table
  - [ ] Create indexes: ivfflat on embedding

- [ ] Create migration: `src/db/migrations/versions/003_add_incident_memory.py`
  - [ ] Create incidents table
  - [ ] Create incident_actions table
  - [ ] Create indexes
  - [ ] Test: `alembic upgrade head` (no errors)

- [ ] Create `src/db/incident_memory.py`
  - [ ] `IncidentMemory` class
  - [ ] `store_incident()` method
  - [ ] `find_similar()` method (pgvector cosine search)
  - [ ] `find_similar_by_root_cause()` method
  - [ ] `suggest_fixes()` method
  - [ ] `resolve_incident()` method
  - [ ] `get_statistics()` method
  - [ ] `get_recurring_issues()` method

- [ ] Create API routes: `src/api/routes/incidents.py`
  - [ ] POST /api/incidents/store
  - [ ] GET /api/incidents/similar
  - [ ] GET /api/incidents/suggest-fixes
  - [ ] POST /api/incidents/resolve/{id}
  - [ ] GET /api/incidents/statistics
  - [ ] GET /api/incidents/recurring-issues

- [ ] Create schemas: `src/api/schemas/incidents.py`
  - [ ] `IncidentCreateRequest`
  - [ ] `IncidentResponse`
  - [ ] `SuggestionsResponse`
  - [ ] `StatisticsResponse`

- [ ] Create tests: `tests/db/test_incident_memory.py`
  - [ ] `test_store_incident` ✓
  - [ ] `test_find_similar` ✓
  - [ ] `test_suggest_fixes` ✓
  - [ ] `test_statistics` ✓
  - [ ] `test_recurring_issues` ✓
  - Run: `pytest tests/db/test_incident_memory.py -v`
  - [ ] All tests passing ✓

- [ ] Create API tests: `tests/api/test_incident_api.py`
  - [ ] `test_store_incident_endpoint` ✓
  - [ ] `test_find_similar_endpoint` ✓
  - [ ] `test_suggest_fixes_endpoint` ✓
  - Run: `pytest tests/api/test_incident_api.py -v`
  - [ ] All tests passing ✓

**Wednesday:**
- [ ] Integration testing
  - [ ] Store 50 incidents via API
  - [ ] Search for similar ones
  - [ ] Verify cosine distances < 0.3 for similar
  - [ ] Get suggestions for new embedding
  - [ ] Latency < 100ms for k=5 search

- [ ] Dashboard component
  - [ ] Add "Similar incidents" widget
  - [ ] Add "Suggested fixes" panel
  - [ ] Add "Incident history" table

**Thursday:**
- [ ] Load testing
  - [ ] Store 1000 incidents
  - [ ] Query with k=5: latency < 100ms?
  - [ ] Query with k=20: latency < 200ms?

- [ ] Code review + optimization
  - [ ] pgvector index tuning if needed
  - [ ] Query optimization

**Friday:**
- [ ] Staging deployment
  - [ ] Deploy database migration
  - [ ] Deploy API routes
  - [ ] Verify in staging

- [ ] Documentation
  - [ ] Write: `docs/incident_memory_schema.md`
  - [ ] API examples for incident search

---

## ✅ WEEK 1 COMPLETION CRITERIA

**All of the following must be TRUE:**

- [ ] Embedding extraction working (Gap 1)
  - [ ] `embed_sequence()` returns (8,) shape ✓
  - [ ] Inference latency + embedding < 50ms ✓
  - [ ] API responses include embeddings ✓

- [ ] Incident memory working (Gap 2)
  - [ ] 50+ incidents stored in pgvector ✓
  - [ ] Similarity search: <100ms for k=5 ✓
  - [ ] Similar incidents cluster correctly ✓
  - [ ] Suggest fixes returns reasonable suggestions ✓

- [ ] Tests passing
  - [ ] Embedding tests: 7/7 passing ✓
  - [ ] Incident memory tests: 5/5 passing ✓
  - [ ] API tests: 5/5 passing ✓

- [ ] Staged to production
  - [ ] No errors in staging ✓
  - [ ] Performance acceptable ✓
  - [ ] Ready to deploy to prod (do it slowly, monitor) ✓

**SIGN-OFF:** Can proceed to Week 2? YES / NO

If NO, what's blocking? ___________________________________

---

## 🗓️ WEEK 2: ONLINE LEARNING + ENSEMBLE DETECTION

### Gap 3: Online Learning (2 days)

**Monday-Tuesday:**

- [ ] Create `src/models/replay_buffer.py`
  - [ ] `ReplayBuffer` class
  - [ ] `add()` method
  - [ ] `add_batch()` method
  - [ ] `sample()` method (uniform)
  - [ ] `sample_prioritized()` method
  - [ ] `sample_by_label()` method
  - [ ] `get_statistics()` method
  - [ ] `AnomalyBuffer` subclass
  - [ ] `SeasonalBuffer` subclass

- [ ] Create `src/models/online_learner.py`
  - [ ] `OnlineLearner` class
  - [ ] `observe()` method
  - [ ] `_update_step()` method
  - [ ] `predict()` method
  - [ ] `get_statistics()` method
  - [ ] `needs_retraining()` method
  - [ ] `OnlineLearningManager` class

- [ ] Create `src/models/catastrophic_forgetting.py`
  - [ ] `ElasticWeightConsolidation` (EWC) class
  - [ ] `compute_fisher()` method
  - [ ] `penalty()` method
  - [ ] `SynapticIntelligence` (SI) class

- [ ] Create `src/models/drift_detector.py`
  - [ ] `DriftDetector` class
  - [ ] `update()` method
  - [ ] Drift detection logic

- [ ] Create tests: `tests/models/test_online_learner.py`
  - [ ] `test_replay_buffer_add`
  - [ ] `test_replay_buffer_sample`
  - [ ] `test_prioritized_sampling`
  - [ ] `test_online_learner_observe`
  - [ ] `test_online_learner_predict`
  - [ ] `test_catastrophic_forgetting_penalty`
  - [ ] `test_drift_detection`
  - Run: `pytest tests/models/test_online_learner.py -v`
  - [ ] All tests passing ✓

**Wednesday:**

- [ ] Integration testing
  - [ ] 1000 sequences streaming in
  - [ ] Loss decreasing? ✓
  - [ ] EWC penalty preventing forgetting? ✓
  - [ ] Drift detection triggered appropriately? ✓
  - [ ] No performance degradation? ✓

- [ ] Dashboard component
  - [ ] Add "Learning statistics" widget
  - [ ] Show: sequences observed, updates performed
  - [ ] Show: loss trend

---

### Gap 4: Ensemble Anomaly Detection (3 days)

**Monday-Wednesday:**

- [ ] Create `src/models/ensemble_detector.py`
  - [ ] `IsolationForestDetector` class
  - [ ] `StatisticalDetector` class (Mahalanobis)
  - [ ] `CorrelationDetector` class (physics constraints)
  - [ ] `EnsembleAnomalyDetector` class
  - [ ] `detect()` method returning 4 scores + weighted vote
  - [ ] Configurable detector weights

- [ ] Create tests: `tests/models/test_ensemble_detector.py`
  - [ ] `test_isolation_forest_detection`
  - [ ] `test_statistical_detection`
  - [ ] `test_correlation_detection`
  - [ ] `test_ensemble_vote`
  - [ ] `test_ensemble_agreement`
  - Run: `pytest tests/models/test_ensemble_detector.py -v`
  - [ ] All tests passing ✓

**Wednesday:**

- [ ] Integration testing
  - [ ] Run ensemble on synthetic test set
  - [ ] Compare: LSTM-VAE alone vs ensemble
  - [ ] Is catch rate improved? (Target: 25% better) ✓
  - [ ] False positive rate acceptable? ✓
  - [ ] Latency acceptable? (<50ms) ✓

- [ ] Dashboard update
  - [ ] Show all 4 detector scores
  - [ ] Show ensemble score
  - [ ] Show detector agreement %

---

## ✅ WEEK 2 COMPLETION CRITERIA

- [ ] Online Learning
  - [ ] Model loss decreasing over 1000 sequences ✓
  - [ ] EWC preventing catastrophic forgetting ✓
  - [ ] No catastrophic forgetting (test loss preserved) ✓
  - [ ] Drift detection working ✓

- [ ] Ensemble Detection
  - [ ] 4 detectors implemented ✓
  - [ ] Ensemble catch rate 25% better than LSTM-VAE ✓
  - [ ] False positive rate < 20% ✓
  - [ ] Latency < 50ms ✓

- [ ] Tests passing
  - [ ] Online learner tests: 7/7 ✓
  - [ ] Ensemble tests: 5/5 ✓

- [ ] Dashboard
  - [ ] Learning stats visible ✓
  - [ ] 4 detector scores visible ✓
  - [ ] Ensemble score visible ✓

**SIGN-OFF:** Can proceed to Week 3? YES / NO

---

## 🗓️ WEEK 3: CAUSAL DISCOVERY + UNCERTAINTY

### Gap 5: Causal Discovery (2 days)

**Monday-Tuesday:**

- [ ] Create `src/models/causal_analyzer.py`
  - [ ] `CausalAnalyzer` class
  - [ ] `analyze_anomaly()` method using Granger causality
  - [ ] Root cause hypotheses for:
    - [ ] Heating failure
    - [ ] Ventilation failure
    - [ ] CO2 supply failure
    - [ ] Power issues
  - [ ] Constraint violation checker

- [ ] Tests: `tests/models/test_causal_analyzer.py`
  - [ ] `test_detect_heating_failure`
  - [ ] `test_detect_ventilation_failure`
  - [ ] `test_detect_co2_failure`
  - [ ] `test_granger_causality`
  - [ ] All tests passing ✓

- [ ] Integration with incident memory
  - [ ] Add causal analysis to `suggest_fixes()`
  - [ ] Root cause suggestions in API response

---

### Gap 7: Uncertainty Quantification (2 days)

**Monday-Tuesday:**

- [ ] Create `src/models/uncertainty.py`
  - [ ] `UncertaintyQuantifier` class
  - [ ] `predict_with_bounds()` method
  - [ ] Confidence intervals
  - [ ] Uncertainty from VAE logvar

- [ ] Tests: `tests/models/test_uncertainty.py`
  - [ ] `test_confidence_intervals`
  - [ ] `test_uncertainty_calibration`
  - [ ] All tests passing ✓

- [ ] Update inference pipeline
  - [ ] All predictions include confidence bounds
  - [ ] API response: lower_bound, upper_bound

---

## ✅ WEEK 3 COMPLETION CRITERIA

- [ ] Causal Discovery
  - [ ] Root cause analysis working ✓
  - [ ] 80%+ accuracy vs ground truth ✓
  - [ ] Granger causality implemented ✓
  - [ ] Integrated with incident memory ✓

- [ ] Uncertainty
  - [ ] Confidence intervals on all predictions ✓
  - [ ] Uncertainty < 0.15 for confident predictions ✓
  - [ ] Uncertainty > 0.25 for uncertain ones ✓

- [ ] Dashboard
  - [ ] Shows root cause hypothesis ✓
  - [ ] Shows confidence bounds ✓

**SIGN-OFF:** Can proceed to Week 4? YES / NO

---

## 🗓️ WEEK 4: FEW-SHOT + ACTIVE LEARNING

### Gap 6: Few-Shot Learning (3 days)

**Monday-Wednesday:**

- [ ] Create `src/models/few_shot_adapter.py`
  - [ ] `PrototypicalNetworkAdapter` class
  - [ ] `adapt()` method on 100 sequences
  - [ ] Rapid customer onboarding

- [ ] Tests: `tests/models/test_few_shot.py`
  - [ ] `test_few_shot_adaptation_100_samples`
  - [ ] `test_prototype_computation`
  - [ ] Compare: cold-start vs few-shot
  - [ ] All tests passing ✓

- [ ] Benchmark
  - [ ] 1 customer, 100 sequences: accuracy after adaptation?
  - [ ] Compare to 1000 sequence baseline
  - [ ] Target: 80% of baseline performance with 10% data

---

### Gap 8: Active Learning (2 days)

**Wednesday-Thursday:**

- [ ] Create `src/models/active_learning.py`
  - [ ] `ActiveLearner` class
  - [ ] `select_for_labeling()` method
  - [ ] `learn_from_feedback()` method
  - [ ] Threshold auto-tuning

- [ ] Create `src/api/routes/feedback.py`
  - [ ] POST /api/feedback/mark-anomaly
  - [ ] POST /api/feedback/tune-threshold
  - [ ] GET /api/feedback/statistics

- [ ] Dashboard component
  - [ ] "Mark as true/false positive" buttons
  - [ ] "Feedback statistics" widget
  - [ ] Threshold adjustment slider

- [ ] Tests: `tests/models/test_active_learning.py`
  - [ ] `test_select_for_labeling`
  - [ ] `test_learn_from_feedback`
  - [ ] All tests passing ✓

---

## ✅ WEEK 4 COMPLETION CRITERIA

- [ ] Few-Shot Learning
  - [ ] Adapts on 100 sequences ✓
  - [ ] 80%+ of baseline performance ✓
  - [ ] Faster adaptation than before ✓

- [ ] Active Learning
  - [ ] Feedback loop working ✓
  - [ ] Operator marks anomalies ✓
  - [ ] Model learns from feedback (loss improves) ✓
  - [ ] Threshold auto-tuning working ✓

- [ ] Dashboard
  - [ ] Feedback UI working ✓
  - [ ] Threshold adjustment working ✓

**SIGN-OFF:** Can proceed to Week 5? YES / NO

---

## 🗓️ WEEK 5: INTEGRATION + A/B TESTING

### Integration Testing

**Monday:**

- [ ] End-to-end test
  - [ ] Simulate 2 customers × 4 weeks data
  - [ ] Verify full pipeline:
    - Embeddings ✓
    - Incident memory ✓
    - Online learning ✓
    - Ensemble detection ✓
    - Causal analysis ✓
    - Uncertainty ✓
    - Feedback loop ✓
  - [ ] All succeed without errors ✓

### A/B Testing Framework

**Tuesday-Wednesday:**

- [ ] Create `src/models/ab_testing.py`
  - [ ] Model versioning
  - [ ] Traffic splitting (10%, 25%, 50%, 100%)
  - [ ] Metrics collection
  - [ ] Winner detection (95% CI)

- [ ] Design experiment
  - [ ] Control: Current LSTM-VAE
  - [ ] Treatment: Full ensemble + online learning
  - [ ] Metrics:
    - False positive rate
    - Detection latency
    - MTTR (mean time to resolution)
  - [ ] Duration: 2 weeks
  - [ ] Sample size: 50+ anomalies per group

- [ ] Prepare rollout plan
  - [ ] Week 1: 10% traffic to new stack
  - [ ] Week 2: 25% traffic
  - [ ] Week 3: 50% traffic
  - [ ] Week 4: 100% traffic (if winning)

### Documentation

**Thursday-Friday:**

- [ ] Write architecture docs
  - [ ] System diagram
  - [ ] Data flow
  - [ ] API reference
  - [ ] Integration guide

- [ ] Write operations runbook
  - [ ] Deployment steps
  - [ ] Troubleshooting
  - [ ] Monitoring
  - [ ] Rollback procedures

- [ ] Write user guide
  - [ ] New features
  - [ ] Dashboard walkthroughs
  - [ ] FAQ

---

## ✅ WEEK 5 COMPLETION CRITERIA

- [ ] Integration Testing
  - [ ] End-to-end test passing ✓
  - [ ] All components working together ✓
  - [ ] 0 integration errors ✓

- [ ] A/B Testing
  - [ ] Framework implemented ✓
  - [ ] Experiment design finalized ✓
  - [ ] Rollout plan approved ✓

- [ ] Documentation
  - [ ] Architecture docs complete ✓
  - [ ] Operations runbook complete ✓
  - [ ] User guide complete ✓

**SIGN-OFF:** Can proceed to Week 6? YES / NO

---

## 🗓️ WEEK 6: QA + DEPLOYMENT

### QA

**Monday-Tuesday:**

- [ ] Regression testing
  - [ ] All existing features still work ✓
  - [ ] API endpoints respond correctly ✓
  - [ ] Dashboard loads ✓

- [ ] Load testing
  - [ ] 10 customers × 1000 seqs/day
  - [ ] No memory leaks ✓
  - [ ] No performance degradation ✓

- [ ] Stress testing
  - [ ] Sudden traffic spike (10x) ✓
  - [ ] 72-hour stability test ✓
  - [ ] No crashes ✓

### Performance Optimization

**Wednesday:**

- [ ] Profiling
  - [ ] Inference latency: <50ms? ✓
  - [ ] pgvector search: <100ms? ✓
  - [ ] Online learning update: <10ms? ✓
  - [ ] Embedding extraction: <5ms? ✓

- [ ] Optimization
  - [ ] If slow, optimize
  - [ ] Check: GPU usage, memory, CPU

### Security & Deployment

**Thursday:**

- [ ] Security review
  - [ ] No hardcoded credentials ✓
  - [ ] Dependency vulnerabilities: 0? ✓
  - [ ] API authentication working ✓

- [ ] Blue-green deployment
  - [ ] Setup blue-green infrastructure
  - [ ] Test: Switch between versions
  - [ ] Prepare rollback procedure

**Friday:**

- [ ] Soft launch
  - [ ] Deploy to internal team
  - [ ] Gather feedback
  - [ ] Fix any issues

- [ ] Team training
  - [ ] Operator guide: new features
  - [ ] Dashboard walkthroughs
  - [ ] FAQ document

---

## ✅ WEEK 6 COMPLETION CRITERIA

- [ ] Testing
  - [ ] Regression: all tests passing ✓
  - [ ] Load test: 10 customers, no degradation ✓
  - [ ] Stress test: 72 hours, no crashes ✓

- [ ] Performance
  - [ ] Inference: <50ms ✓
  - [ ] pgvector: <100ms ✓
  - [ ] Online learning: <10ms ✓

- [ ] Deployment Ready
  - [ ] Blue-green setup ✓
  - [ ] Rollback procedures tested ✓
  - [ ] Team trained ✓
  - [ ] Documentation complete ✓

**SIGN-OFF:** Ready for beta launch? YES / NO

---

## 📊 CUMULATIVE TEST COUNT

| Week | Gap | Tests Added | Cumulative |
|------|-----|------------|-----------|
| 1 | 1-2 | 12 | 12 |
| 2 | 3-4 | 12 | 24 |
| 3 | 5,7 | 8 | 32 |
| 4 | 6-8 | 8 | 40 |
| 5 | Integration | 20 | 60 |
| 6 | QA | 40+ | 100+ |

**Target:** 100+ tests, >80% coverage ✓

---

## 🏆 SUCCESS DASHBOARD

Print & fill in weekly:

```
WEEK 1: [    ] / 100 %
  Gap 1 embedding: ___/7 tests ✓
  Gap 2 memory: ___/8 tests ✓
  Integration: ___/5 tests ✓

WEEK 2: [    ] / 100 %
  Gap 3 online learning: ___/7 tests ✓
  Gap 4 ensemble: ___/5 tests ✓
  Performance target met: YES/NO

WEEK 3: [    ] / 100 %
  Gap 5 causal: ___/4 tests ✓
  Gap 7 uncertainty: ___/3 tests ✓
  Accuracy vs baseline: ___% (target: 80%)

WEEK 4: [    ] / 100 %
  Gap 6 few-shot: ___/3 tests ✓
  Gap 8 active learning: ___/3 tests ✓
  Feedback loop working: YES/NO

WEEK 5: [    ] / 100 %
  End-to-end test: PASS/FAIL
  A/B framework: READY/NOT
  Documentation: COMPLETE/INCOMPLETE

WEEK 6: [    ] / 100 %
  100+ tests passing: YES/NO
  Performance targets: YES/NO
  Ready for beta: YES/NO
```

---

## 📞 ESCALATION POINTS

### If blocked:
1. Check: Do I have all dependencies installed?
2. Check: Is the database running?
3. Check: Are there compilation errors?
4. Escalate: Describe issue + error message

### Decision gates (MUST PASS):
- Week 1: Embeddings + memory working → Proceed
- Week 2: Online learning stable → Proceed
- Week 3: Causal + uncertainty ready → Proceed
- Week 4: Feedback loop working → Proceed
- Week 5: Integration tests passing → Proceed
- Week 6: 100+ tests passing → LAUNCH ✓

---

**Good luck! You've got a solid plan. Stick to the timeline, test thoroughly, and you'll have a production-ready self-training platform in 6 weeks. 🚀**

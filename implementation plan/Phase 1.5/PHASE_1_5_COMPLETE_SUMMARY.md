# Phase 1.5: Model Adaptation Infrastructure
## Complete 6-Week Breakdown (42 Days)

---

## 📋 **Overview**

Phase 1.5 transforms your platform from **"generic model"** to **"per-customer, production-ready anomaly detector"**.

After Phase 1.5, customers can upload real data, model learns their patterns, and detects anomalies specific to their greenhouse.

---

## 📂 **Files Created (6 Documents)**

### **Week 1: Baseline Analysis & Transfer Learning (Parallel, 25 min)**
📄 **`PHASE_1_5_WEEK_1.md`**

**Agents:** architect, ml-engineer (×2), qa-engineer (parallel)

**What they build:**
- Baseline analyzer (extract customer normal patterns)
- Transfer learning pipeline (fine-tune Week 1 model)
- Model evaluator (compare Week 1 vs adapted)
- 3 synthetic customer datasets (tomato, lettuce, cucumber)
- 17 tests

**Outputs:**
- `docs/architecture/baseline_transfer_learning_design.md`
- `src/models/baseline_analyzer.py`
- `src/models/transfer_learner.py`
- `src/models/model_evaluator.py`
- `data/synthetic_customers/` (3 profiles)
- Tests passing (17 total)

**Success:** Week 1 model 20%+ better on customer data

---

### **Week 2: Per-Customer Models & Registry (Sequential, 20 min)**
📄 **`PHASE_1_5_WEEK_2.md`**

**Agent:** backend-engineer, qa-engineer

**What they build:**
- Model registry (track all versions)
- Per-customer training pipeline
- Database migrations
- Updated inference (use customer model, not baseline)
- API endpoints for training

**Outputs:**
- `src/models/model_registry.py`
- `src/db/migrations/002_add_model_tables.py`
- `src/api/routes/models.py`
- `src/models/training_pipeline.py`
- Tests passing (13 total)

**Success:** Each customer gets own model, can train independently

---

### **Week 3: Seasonal Models (Sequential, 20 min)**
📄 **`PHASE_1_5_WEEK_3.md`**

**Agent:** ml-engineer, backend-engineer, qa-engineer

**What they build:**
- Seasonal detector (which season is it?)
- Seasonal trainer (separate models per season)
- Auto-select seasonal model at inference
- Dashboard: show seasonal variants

**Outputs:**
- `src/models/seasonal_detector.py`
- `src/models/seasonal_trainer.py`
- Updated inference (use season-specific model)
- Tests passing (13 total)

**Success:** Summer model ≠ Winter model, auto-detection works

---

### **Week 4: Feedback Loop & Auto-Tuning (Sequential, 20 min)**
📄 **`PHASE_1_5_WEEK_4.md`**

**Agent:** backend-engineer, qa-engineer

**What they build:**
- Feedback aggregator (collect operator marks)
- Threshold optimizer (calculate best threshold)
- Dashboard: "Mark Anomalies" page
- API endpoints for feedback

**Outputs:**
- `src/models/feedback_aggregator.py`
- `src/models/threshold_optimizer.py`
- `src/api/routes/feedback.py`
- `src/dashboard/pages/mark_anomalies.py`
- Tests passing (13 total)

**Success:** Operator marks false positives, threshold auto-adjusts

---

### **Week 5: A/B Testing & Performance (Sequential, 20 min)**
📄 **`PHASE_1_5_WEEK_5.md`**

**Agent:** ml-engineer, backend-engineer, qa-engineer

**What they build:**
- A/B testing framework (compare models safely)
- Traffic split (10/90 → 50/50)
- Statistical significance testing
- Dashboard: A/B test results
- Performance metrics (latency, accuracy)

**Outputs:**
- `src/models/ab_tester.py`
- `src/api/routes/ab_tests.py`
- `src/dashboard/pages/model_comparison.py`
- Tests passing (11 total)

**Success:** Can safely test new models, declare winner

---

### **Week 6: Priva Integration & Production (Sequential, 25 min)**
📄 **`PHASE_1_5_WEEK_6.md`**

**Agents:** architect, ml-engineer, backend-engineer, devops-lead, qa-engineer, code-reviewer

**What they build:**
- Priva API client (real, not stub)
- Automated training scheduler
- Production logging/monitoring
- Health checks
- Production deployment guide
- Code review + ADRs

**Outputs:**
- `src/vendors/priva_client.py` (updated)
- `src/models/training_scheduler.py`
- `src/models/priva_connector.py`
- `src/dashboard/pages/data_pipeline.py`
- Health checks + Prometheus metrics
- Tests passing (18 total)
- `docs/PRODUCTION_DEPLOYMENT.md`
- `docs/architecture/ADR-007-phase-1-5.md`

**Success:** Automated data pipeline, production-ready, fully tested

---

## 🚀 **How to Execute Phase 1.5**

### **Step 1: Start Week 1 (Now)**
```bash
cd ~/Projects/ai-agronomist
claude

# At ❯ prompt:
# Copy "ORCHESTRATOR SUBMISSION TEXT" from PHASE_1_5_WEEK_1.md
# Paste and press Enter
```

### **Step 2-6: Wait → Submit Next**
```
Week 1 complete (25 min) → Submit Week 2 (wait 20 min)
Week 2 complete → Submit Week 3 (wait 20 min)
Week 3 complete → Submit Week 4 (wait 20 min)
Week 4 complete → Submit Week 5 (wait 20 min)
Week 5 complete → Submit Week 6 (wait 25 min)
Week 6 complete → PHASE 1.5 DONE! ✅
```

**Total execution time:** ~130 minutes (2+ hours of waiting, agents work)

---

## 📊 **Timeline Summary**

```
Day 1:    Execute Week 1-2 (45 min)
Day 2:    Execute Week 3-4 (40 min)
Day 3:    Execute Week 5-6 (45 min)
Total:    ~3 days of execution
Result:   Production-ready per-customer anomaly detector
```

---

## ✅ **What You'll Have After Phase 1.5**

### **1. Baseline Analysis ✅**
- Understand each customer's normal operation
- Extract sensor ranges, correlations, drift patterns
- Foundation for all downstream tasks

### **2. Transfer Learning ✅**
- Week 1 model (Week 1) + customer data = customer-specific model
- 20-30% improvement on customer data
- Fast: 30-60 min training per customer

### **3. Per-Customer Models ✅**
- Customer A model ≠ Customer B model
- Each trained on their own data
- Deployment-ready with versioning

### **4. Seasonal Variants ✅**
- Summer model ≠ Winter model
- Auto-detect season
- Better accuracy across all seasons

### **5. Feedback Loop ✅**
- Operator marks false positives in dashboard
- False positive rate tracked
- Model improves from feedback

### **6. Auto-Threshold Tuning ✅**
- Threshold adjusts based on feedback
- Reduces false positives automatically
- Operator doesn't need to manually tune

### **7. A/B Testing ✅**
- Compare models safely (10/90 traffic split)
- Latency and accuracy metrics
- Statistical significance testing

### **8. Priva Integration ✅**
- Real Priva API data (not CSV)
- Automatic polling every 30 min
- No manual uploads needed

### **9. Automated Training ✅**
- Daily training runs automatically
- Weekly re-training
- Feedback-triggered training
- Failed job retry + alerts

### **10. Production Ready ✅**
- Structured JSON logging
- Prometheus metrics
- Health checks
- Monitoring/alerting
- Deployment guide
- Code review + ADRs

---

## 📁 **File Structure After Phase 1.5**

```
src/
├── models/
│   ├── lstm_vae.py (Week 1)
│   ├── inference.py (Week 1, updated)
│   ├── training.py (Week 1)
│   ├── baseline_analyzer.py ⭐ NEW
│   ├── transfer_learner.py ⭐ NEW
│   ├── model_evaluator.py ⭐ NEW
│   ├── model_registry.py ⭐ NEW
│   ├── training_pipeline.py ⭐ NEW
│   ├── seasonal_detector.py ⭐ NEW
│   ├── seasonal_trainer.py ⭐ NEW
│   ├── feedback_aggregator.py ⭐ NEW
│   ├── threshold_optimizer.py ⭐ NEW
│   ├── ab_tester.py ⭐ NEW
│   ├── training_scheduler.py ⭐ NEW
│   └── priva_connector.py ⭐ NEW
├── vendors/
│   ├── priva_client.py (Week 2, UPDATED Week 6)
│   └── csv_adapter.py
├── dashboard/
│   └── pages/
│       ├── ... (Week 2)
│       ├── mark_anomalies.py ⭐ NEW
│       ├── model_comparison.py ⭐ NEW
│       └── data_pipeline.py ⭐ NEW
└── db/
    └── migrations/
        └── 002_add_model_tables.py ⭐ NEW

docs/
├── architecture/
│   ├── baseline_transfer_learning_design.md ⭐ NEW
│   ├── seasonal_models_design.md ⭐ NEW
│   ├── priva_integration_design.md ⭐ NEW
│   ├── training_pipeline_design.md ⭐ NEW
│   └── ADR-007-phase-1-5.md ⭐ NEW
├── review/
│   └── phase_1_5_review.md ⭐ NEW
└── PRODUCTION_DEPLOYMENT.md ⭐ NEW

tests/
├── models/
│   ├── test_baseline_analyzer.py ⭐ NEW
│   ├── test_transfer_learner.py ⭐ NEW
│   ├── test_seasonal_detector.py ⭐ NEW
│   ├── test_seasonal_trainer.py ⭐ NEW
│   ├── test_feedback_aggregator.py ⭐ NEW
│   ├── test_threshold_optimizer.py ⭐ NEW
│   ├── test_ab_tester.py ⭐ NEW
│   └── test_training_scheduler.py ⭐ NEW
├── vendors/
│   └── test_priva_client.py (Week 2, UPDATED Week 6)
├── api/
│   ├── test_models.py ⭐ NEW
│   ├── test_feedback.py ⭐ NEW
│   └── test_ab_tests.py ⭐ NEW
└── integration/
    ├── test_training_end_to_end.py ⭐ NEW
    ├── test_seasonal_inference.py ⭐ NEW
    ├── test_full_pipeline.py ⭐ NEW
    └── ...

data/
└── synthetic_customers/ ⭐ NEW
    ├── customer_tomato/
    │   ├── normal.csv
    │   └── anomalies.csv
    ├── customer_lettuce/
    │   ├── normal.csv
    │   └── anomalies.csv
    └── customer_cucumber/
        ├── normal.csv
        └── anomalies.csv
```

---

## 🎯 **Success Metrics**

| Metric | Target | Verification |
|--------|--------|--------------|
| **Baseline accuracy** | Extract ranges ±5% | Test: `test_baseline_ranges` |
| **Transfer learning** | 20% improvement | Test: `test_fine_tune_improves_accuracy` |
| **Per-customer models** | 3+ independent | Test: `test_multiple_customer_models` |
| **Seasonal models** | All 4 seasons | Test: `test_all_seasons_present` |
| **False positive rate** | < 15% (after feedback) | Dashboard metrics |
| **A/B test significance** | 95% confidence | Test: `test_statistical_significance` |
| **Inference latency** | < 500ms | Prometheus metrics |
| **Test coverage** | > 85% | Coverage report |
| **Priva integration** | Poll every 30 min | Health check: `/health/priva` |
| **Training automation** | Daily 00:00 UTC | Scheduler logs |

---

## 💡 **Key Concepts**

| Concept | What | Why |
|---------|------|-----|
| **Baseline Analysis** | Extract customer's normal patterns | Understand their greenhouse |
| **Transfer Learning** | Reuse encoder, fine-tune decoder | Fast, efficient adaptation |
| **Per-Customer Models** | Each customer gets own model | Different greenhouses ≠ same patterns |
| **Seasonal Models** | Summer/winter variants | Temperature range changes by season |
| **Feedback Loop** | Operator marks false positives | Threshold improves over time |
| **A/B Testing** | Compare models safely | Avoid deploying worse models |
| **Priva Integration** | Real data, not synthetic | Production deployable |
| **Automation** | Daily retraining | No manual intervention |

---

## 🔄 **Data Flow (After Phase 1.5)**

```
Customer's Greenhouse (Priva Sensors)
    ↓ (automated polling every 30 min)
Priva Cloud
    ↓ (OAuth 2.0)
PrivaConnector (AI Agronomist)
    ↓
Store in PostgreSQL
    ↓
Daily 00:00 UTC: TrainingScheduler wakes up
    ↓
Check: Is new data available?
    ├─ No  → Sleep until tomorrow
    └─ Yes → Trigger PerCustomerTrainingPipeline
        ↓
        1. Load customer's baseline
        2. Load past week's data
        3. Fine-tune model (30 min training)
        4. Evaluate: better than current?
        ├─ No  → Keep current model
        └─ Yes → A/B test new model
                ↓
                Traffic: 10% on new, 90% on old
                Duration: 3 days
                ↓
                Compare metrics: latency, accuracy, FP rate
                ↓
                Is new better? (95% confidence)
                ├─ Yes → Deploy! 🎉
                │   - Update ModelRegistry
                │   - Send notification
                │   - New inference uses updated model
                └─ No → Keep old, log reason
        ↓
Operator uses dashboard:
    - Sees anomalies
    - Charts show sensor readings
    - Marks false positives: "This is normal for us"
    ↓
Feedback aggregated:
    - False positive rate calculated
    - Threshold optimizer recommends adjustment
    - Threshold auto-tunes
    ↓
Next week: Better model!
    - Fewer false positives
    - Catches real anomalies
    - Operator happier 😊
```

---

## 🚀 **Next After Phase 1.5**

### **Option A: Phase 2 (Advanced Modeling)**
- Ensemble methods
- Multiple anomaly detectors
- Confidence scoring
- Duration: 4 weeks

### **Option B: Phase 3 (Multi-Vendor)**
- Hoogendoorn API
- Custom IoT
- Unified pipeline
- Duration: 4 weeks

### **Option C: Phase 4 (Autonomous Control)**
- AI recommends corrective actions
- Operator approval before execution
- Emergency protocols
- Duration: 8 weeks

---

## 📊 **Investment Required**

| Resource | Weeks 1-6 |
|----------|-----------|
| **Execution time** | ~3 days (130 min total) |
| **Agent hours** | ~24 hours (agents work in parallel) |
| **Your involvement** | 5 min per week (approve deploys) |
| **Testing** | ~90 tests, all automated |
| **Documentation** | 4 design docs + deployment guide |

---

## ✨ **The Magic Moment**

**Before Phase 1.5:**
- Model trained on synthetic data
- "Generic" baseline
- High false positive rate
- Manual threshold tuning

**After Phase 1.5:**
- Model trained on customer's data
- Knows their greenhouse patterns
- Low false positive rate
- Auto-tuning from feedback
- **Automated pipeline** (Priva → training → inference)

---

## 🎬 **Ready to Execute?**

1. ✅ Confirm you're ready for Phase 1.5
2. ✅ All 6 week documents ready
3. ✅ Synthetic data included (no real data needed)
4. ✅ Submission format: paste text from each week
5. ✅ Timeline: ~3 days execution (distributed over 6 weeks)

---

**Next Step:** Open **PHASE_1_5_WEEK_1.md** and submit Week 1! 🚀

Phase 1.5 is where your platform becomes **actually useful**. ✨

After this, you'll have a production-ready anomaly detector that:
- Learns from customer data
- Detects anomalies specific to their greenhouse
- Auto-improves from feedback
- Runs completely automated

Let's do this! 🎯

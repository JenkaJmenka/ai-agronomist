# Phase 1.5: Model Adaptation Infrastructure
## Transfer Learning, Per-Customer Models, Feedback Loop (6 Weeks)

---

## 🎯 **Phase 1.5 Vision**

Transform from **"generic model trained on synthetic data"** to **"custom models that learn YOUR greenhouse"**

```
Week 1 Model (synthetic baseline)
    ↓
Customer uploads real data (2 weeks normal operation)
    ↓
Phase 1.5: Baseline analysis + transfer learning
    ↓
NEW Model (knows YOUR greenhouse's patterns)
    ↓
Upload new data → Detects deviations from YOUR normal (90%+ accurate)
```

---

## 📋 **What Phase 1.5 Includes**

### **Week 1: Baseline Analysis & Transfer Learning**
- Analyze greenhouse sensor patterns (ranges, correlations)
- Fine-tune Week 1 model on customer data (30-60 min training)
- Evaluate adapted model vs Week 1 baseline

### **Week 2: Per-Customer Models & Registry**
- Train separate models for different customer types
- Model versioning (track which model was deployed when)
- Operator feedback collection UI

### **Week 3: Seasonal Models**
- Different models for summer/winter/spring/fall
- Auto-detect season, use appropriate model
- Temperature/daylight correlation detection

### **Week 4: Feedback Loop & Auto-Tuning**
- Operator marks false positives in dashboard
- Model learns from feedback (fine-tune based on feedback)
- Threshold auto-adjustment

### **Week 5: A/B Testing & Performance**
- Compare multiple model versions
- Test new baselines against current
- Performance metrics (latency, accuracy)

### **Week 6: Priva Integration & Final Polish**
- Priva API integration (replaces manual CSV upload)
- Automated data pipeline (Priva → model training → inference)
- Production hardening

---

## 🏗️ **Architecture**

```
Customer Data (CSV or Priva API)
    ↓
Baseline Analysis Service
├── Detect normal ranges
├── Detect correlations
├── Identify drift patterns
└── Create baseline snapshot
    ↓
Transfer Learning Service
├── Load Week 1 model
├── Fine-tune on customer data
├── Evaluate performance
└── Save customer-specific model
    ↓
Per-Customer Model Registry
├── Customer A → Model A (summer variant)
├── Customer B → Model B (winter variant)
└── Model metadata (training date, accuracy, feedback)
    ↓
Inference Engine
├── Load correct model (customer + season)
├── Predict anomalies
├── Save scores to DB
└── Feedback collection
    ↓
Feedback Loop
├── Operator marks false positives
├── Aggregate feedback
├── Retrain model (optional)
└── Auto-adjust threshold
```

---

## 📊 **Synthetic Data Strategy**

Since you don't have real customer data yet:

### **Create 3 Synthetic Customer Profiles**

**Customer A: Tomato Greenhouse**
- Normal temp: 22-26°C (more stable)
- Normal humidity: 65-75%
- Normal CO2: 400-500 ppm
- Normal light: 800-1000 lux

**Customer B: Lettuce Greenhouse**
- Normal temp: 18-22°C (cooler)
- Normal humidity: 70-80% (higher)
- Normal CO2: 350-450 ppm
- Normal light: 600-800 lux

**Customer C: Cucumber Greenhouse**
- Normal temp: 24-28°C (warmest)
- Normal humidity: 60-70% (lower)
- Normal CO2: 450-550 ppm
- Normal light: 900-1200 lux

### **Synthetic Anomalies to Detect**

**Heating Failure:** Temperature drops 10°C below normal
**Humidity Spike:** Humidity jumps to 95%+
**CO2 Depletion:** CO2 falls to 200 ppm (photosynthesis malfunction)
**Light Failure:** Light drops to 100 lux (equipment failure)

---

## 🚀 **6-Week Breakdown**

### **WEEK 1: Baseline Analysis & Transfer Learning**

**Tasks:**
1. Create synthetic customer datasets (3 profiles)
2. Implement baseline analysis service
3. Implement transfer learning pipeline
4. Create model evaluation framework
5. Test on synthetic data

**Deliverables:**
- `src/models/baseline_analyzer.py` (detect normal ranges)
- `src/models/transfer_learner.py` (fine-tune model)
- `src/models/model_evaluator.py` (measure improvement)
- Test framework

**Success:** Synthetic customer models created, 20%+ improvement over Week 1 baseline

---

### **WEEK 2: Per-Customer Models & Registry**

**Tasks:**
1. Create model registry (version control)
2. Implement per-customer model training
3. Add operator feedback collection UI
4. Create model metadata schema
5. Test with multiple customers

**Deliverables:**
- `src/models/model_registry.py` (track models)
- `src/db/models.py` (updated: add Model, ModelVersion tables)
- Dashboard page: "Model Management"
- Tests for registry

**Success:** Can train separate models for different customers, track versions

---

### **WEEK 3: Seasonal Models**

**Tasks:**
1. Implement season detection (calendar + temperature)
2. Create seasonal variants (summer/winter models)
3. Add season selector to inference
4. Create seasonal baseline detection
5. Test seasonal switching

**Deliverables:**
- `src/models/seasonal_detector.py` (which season is it?)
- `src/models/seasonal_trainer.py` (train per season)
- Database: add season field to models
- Dashboard: season indicator

**Success:** Inference automatically selects correct seasonal model

---

### **WEEK 4: Feedback Loop & Auto-Tuning**

**Tasks:**
1. Implement feedback UI (mark false positives)
2. Collect feedback in database
3. Aggregate feedback statistics
4. Auto-adjust anomaly threshold
5. Optional: fine-tune model based on feedback

**Deliverables:**
- Dashboard page: "Mark Anomalies" (false positive marking)
- `src/models/feedback_aggregator.py`
- `src/models/threshold_optimizer.py` (auto-tune based on feedback)
- Tests

**Success:** Operator marks false positives, threshold auto-adjusts

---

### **WEEK 5: A/B Testing & Performance**

**Tasks:**
1. Implement A/B testing framework
2. Compare models side-by-side
3. Performance metrics (accuracy, latency)
4. Model comparison dashboard
5. Test A/B switching

**Deliverables:**
- `src/models/ab_tester.py` (compare models)
- Database: A/B test history
- Dashboard page: "Model Comparison"
- Performance metrics API

**Success:** Can run A/B tests, compare models, rollback bad versions

---

### **WEEK 6: Priva Integration & Production Hardening**

**Tasks:**
1. Implement Priva polling scheduler
2. Replace CSV upload with Priva
3. Automated training pipeline
4. Production logging/monitoring
5. Performance optimization

**Deliverables:**
- `src/vendors/priva_client.py` (real implementation, not stub)
- `src/models/training_scheduler.py` (auto-train on schedule)
- Production deployment guide
- Monitoring dashboards

**Success:** Automated data pipeline: Priva → model training → inference

---

## 📁 **Expected File Structure After Phase 1.5**

```
src/
├── models/
│   ├── lstm_vae.py                (Week 1, unchanged)
│   ├── inference.py               (Week 1, unchanged)
│   ├── training.py                (Week 1, unchanged)
│   ├── baseline_analyzer.py        (WEEK 1, NEW)
│   ├── transfer_learner.py         (WEEK 1, NEW)
│   ├── model_evaluator.py          (WEEK 1, NEW)
│   ├── model_registry.py           (WEEK 2, NEW)
│   ├── seasonal_detector.py        (WEEK 3, NEW)
│   ├── seasonal_trainer.py         (WEEK 3, NEW)
│   ├── feedback_aggregator.py      (WEEK 4, NEW)
│   ├── threshold_optimizer.py      (WEEK 4, NEW)
│   ├── ab_tester.py                (WEEK 5, NEW)
│   └── training_scheduler.py       (WEEK 6, NEW)
├── vendors/
│   ├── priva_client.py             (UPDATED: real implementation)
│   └── csv_adapter.py              (unchanged)
├── db/
│   ├── models.py                   (UPDATED: add Model, ModelVersion, Feedback tables)
│   └── migrations/
│       └── 002_add_model_tables.py (NEW)
└── dashboard/
    └── pages/
        ├── model_management.py     (WEEK 2, NEW)
        ├── mark_anomalies.py       (WEEK 4, NEW)
        └── model_comparison.py     (WEEK 5, NEW)

tests/
├── models/
│   ├── test_baseline_analyzer.py    (WEEK 1)
│   ├── test_transfer_learner.py     (WEEK 1)
│   ├── test_seasonal_detector.py    (WEEK 3)
│   ├── test_feedback_loop.py        (WEEK 4)
│   └── test_ab_tester.py            (WEEK 5)

data/
├── synthetic_customers/             (synthetic test data)
│   ├── customer_tomato/
│   ├── customer_lettuce/
│   └── customer_cucumber/
```

---

## 🎯 **Success Metrics**

### **End of Phase 1.5**

✅ **Baseline Analysis**
- Can analyze customer data and detect normal ranges
- Accuracy: ±5% of actual ranges

✅ **Transfer Learning**
- Week 1 baseline accuracy: 70%
- Customer-adapted model accuracy: 85%+
- Improvement: 20%+ better on customer data

✅ **Per-Customer Models**
- Train separate models for 3+ different customer profiles
- Each model specialized for that customer's greenhouse

✅ **Seasonal Models**
- Create summer/winter variants
- Auto-detect season
- Use correct model based on season

✅ **Feedback Loop**
- Operator can mark false positives in dashboard
- Threshold auto-adjusts based on feedback
- Model improves with more feedback

✅ **A/B Testing**
- Can run side-by-side model comparison
- Metrics show improvement/degradation
- Can rollback to previous model

✅ **Priva Integration**
- Real Priva API polling (not stub)
- Automated data pipeline
- No manual CSV upload needed

✅ **Production Ready**
- All tests passing (>85% coverage)
- Monitoring/logging in place
- Deployment guide complete
- Performance optimized

---

## 💡 **Key Design Decisions**

| Decision | Why |
|----------|-----|
| **Transfer Learning** | Reuse Week 1 foundation, adapt to customer |
| **Per-Customer Models** | Each customer has different operating patterns |
| **Seasonal Models** | Greenhouse behavior changes with season |
| **Feedback Loop** | Model improves with operator input |
| **A/B Testing** | Safely test new models before deployment |
| **Automated Pipeline** | Priva replaces manual uploads |

---

## 🔄 **Data Flow**

```
Operator's Greenhouse (Priva IoT sensors)
    ↓ (automatic polling, 15-30 min interval)
Priva API (cloud)
    ↓ (HTTP REST)
AI Agronomist API (fastapi/src/api/main.py)
    ↓
Baseline Analysis Service
├── Detect ranges: 20-28°C temp, 55-75% humidity
├── Detect correlations: temp ↔ humidity (0.8 correlation)
└── Create baseline snapshot
    ↓
Transfer Learning
├── Load Week 1 LSTM-VAE model
├── Fine-tune on customer's 2 weeks normal data (30 min training)
├── Evaluate: accuracy 85% on customer data
└── Save customer-specific weights
    ↓
Inference Service
├── Load customer model + seasonal variant
├── Predict on incoming sequences
├── Anomaly score > threshold? Flag it!
└── Save to database
    ↓
Dashboard
├── Display anomaly timeline
├── Show operator: "This is anomalous (99% confidence)"
├── Operator marks: "False positive - this is normal for us"
├── Feedback aggregated
    ↓
Model Improvement
├── Threshold auto-adjusts (fewer false positives)
├── Model optionally fine-tuned on feedback
└── Next week: Better accuracy!
```

---

## 📊 **Timeline Summary**

```
Week 1 (7 days):   Baseline + transfer learning foundation
Week 2 (7 days):   Per-customer models + registry
Week 3 (7 days):   Seasonal models
Week 4 (7 days):   Feedback loop + auto-tuning
Week 5 (7 days):   A/B testing
Week 6 (7 days):   Priva integration + hardening

Total: 6 weeks (42 days)
Result: Production-ready per-customer anomaly detector ✅
```

---

## 🚀 **What Comes After Phase 1.5**

### **Phase 2 Options**

**Option A: Threshold Tuning (Simple)**
- Manual threshold adjustment in dashboard
- Feedback-based learning
- ~2 weeks

**Option B: Advanced Modeling (Complex)**
- Ensemble methods (combine LSTM-VAE + Isolation Forest)
- Multi-model voting
- Confidence scores
- ~4 weeks

### **Phase 3: Multi-Vendor Support**
- Integrate Hoogendoorn API
- Custom IoT adapters
- Unified data pipeline
- ~4 weeks

### **Phase 4: Autonomous Control (Future)**
- AI recommends corrective actions
- Operator approval before execution
- Emergency protocols
- ~8 weeks

---

## 📝 **Next Steps**

1. **Confirm:** Ready to execute Phase 1.5?
2. **I'll create:** Week-by-week task breakdown (like Week 2)
3. **You'll execute:** 6 weeks of development
4. **Result:** Production-ready per-customer anomaly detector

---

**Ready to build Phase 1.5?** 🚀

This is where your platform becomes actually valuable — models that know YOUR greenhouse's patterns, not generic synthetic patterns.


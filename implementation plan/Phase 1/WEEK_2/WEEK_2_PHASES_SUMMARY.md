# Week 2 - Complete Phase Breakdown
## 6 Phases, 95 Minutes, All Organized

---

## 📋 **Overview**

Week 2 has been split into **6 separate documents**, one per phase.

Each document is ready to submit to your orchestrator. Submit them **sequentially** (wait for each phase to complete before submitting the next).

---

## 📂 **Files Created**

### **Phase 1: Design & Infrastructure (Parallel, 20 min)**
📄 **`WEEK_2_PHASE_1.md`**

**Agents:** architect, backend-engineer, ml-engineer, devops-lead (all simultaneous)

**What they build:**
- API design document
- Dashboard design document
- Vendor abstraction design
- Logging infrastructure setup
- FastAPI app skeleton
- SQLAlchemy models
- Pydantic schemas
- Alembic migrations

**Outputs:**
- `docs/architecture/api_dashboard_design.md`
- `docs/architecture/vendor_adapter_design.md`
- `src/api/main.py`
- `src/db/models.py`
- `src/api/schemas/sensor_data.py`
- `src/models/inference.py` (updated)
- `src/vendors/base.py`
- `src/vendors/csv_adapter.py`
- `src/logging/config.py`
- `src/api/middleware/logging.py`

---

### **Phase 2: API Endpoints & Migrations (Sequential, 20 min)**
📄 **`WEEK_2_PHASE_2.md`**

**Agent:** backend-engineer

**What they build:**
- CSV upload endpoint
- Model inference endpoint
- Health check endpoints
- Database migrations
- Test data seeding

**Outputs:**
- `src/api/routes/health.py`
- `src/api/routes/upload.py`
- `src/api/routes/inference.py`
- `src/db/seeds.py`
- `alembic/versions/001_initial.py`

**Can test:** Upload CSV, get inference results back

---

### **Phase 3: Streamlit Dashboard (Sequential, 20 min)**
📄 **`WEEK_2_PHASE_3.md`**

**Agent:** backend-engineer

**What they build:**
- Multi-page Streamlit app
- Sensor data visualization (time series)
- Anomaly timeline & heatmap
- Threshold adjustment UI
- Performance metrics page

**Outputs:**
- `src/dashboard/app.py`
- `src/dashboard/pages/sensor_data.py`
- `src/dashboard/pages/anomalies.py`
- `src/dashboard/pages/thresholds.py`
- `src/dashboard/pages/metrics.py`
- `src/dashboard/components/charts.py`

**Can test:** `streamlit run src/dashboard/app.py`

---

### **Phase 4: Vendor Abstraction & Priva Stub (Sequential, 15 min)**
📄 **`WEEK_2_PHASE_4.md`**

**Agents:** backend-engineer + qa-engineer

**What they build:**
- Vendor adapter pattern (CSV, Priva stub)
- Abstract base class for data sources
- Priva client stub (Phase 1.5 ready)
- Vendor adapter tests

**Outputs:**
- `src/vendors/__init__.py`
- `src/vendors/base.py` (updated)
- `src/vendors/csv_adapter.py` (updated)
- `src/vendors/priva_client.py`
- `tests/vendors/test_csv_adapter.py`
- `tests/vendors/test_priva_stub.py`

**Can test:** `pytest tests/vendors/ -v`

---

### **Phase 5: Integration Tests (Sequential, 10 min)**
📄 **`WEEK_2_PHASE_5.md`**

**Agent:** qa-engineer

**What they build:**
- API endpoint tests (health, upload, inference)
- End-to-end workflow test
- Coverage reporting
- Test fixtures and utilities

**Outputs:**
- `tests/api/test_health.py`
- `tests/api/test_upload.py`
- `tests/api/test_inference.py`
- `tests/integration/test_end_to_end.py`
- `pytest.ini` (updated)
- `htmlcov/index.html` (coverage report)

**Can test:** `pytest tests/ --cov=src --cov-report=html`

---

### **Phase 6: Code Review & Documentation (Final, 10 min)**
📄 **`WEEK_2_PHASE_6.md`**

**Agent:** code-reviewer

**What they produce:**
- Complete code review report
- Architecture Decision Records (ADRs)
- Deployment guide
- Troubleshooting documentation

**Outputs:**
- `docs/review/week2-review.md`
- `docs/architecture/ADR-004-rest-api.md`
- `docs/architecture/ADR-005-vendor-abstraction.md`
- `docs/architecture/ADR-006-streamlit-dashboard.md`
- `docs/DEPLOYMENT.md`

---

## 🚀 **How to Submit**

### **Step 1: Phase 1 (Now)**
```bash
cd ~/Projects/ai-agronomist
claude

# At ❯ prompt:
# Copy entire content from WEEK_2_PHASE_1.md (the "ORCHESTRATOR SUBMISSION TEXT" section)
# Paste and press Enter
```

### **Step 2: Wait for Phase 1 → Complete**
```
Orchestrator will show: "Phase 1 complete"
All 4 agents (architect, backend-engineer x2, ml-engineer, devops-lead) done
```

### **Step 3: Phase 2**
```bash
# At new ❯ prompt:
# Copy entire content from WEEK_2_PHASE_2.md 
# Paste and press Enter
```

### **Step 4: Wait → Phase 3**
```bash
# Repeat for Phase 3, then Phase 4, then Phase 5, then Phase 6
```

---

## 📊 **Timeline**

```
Now          Submit Phase 1 (wait 20 min)
+20 min      Phase 1 done → Submit Phase 2 (wait 20 min)
+40 min      Phase 2 done → Submit Phase 3 (wait 20 min)
+60 min      Phase 3 done → Submit Phase 4 (wait 15 min)
+75 min      Phase 4 done → Submit Phase 5 (wait 10 min)
+85 min      Phase 5 done → Submit Phase 6 (wait 10 min)
+95 min      Phase 6 done → WEEK 2 COMPLETE ✅
```

**Total: ~95 minutes of waiting (agents work, you watch)**

---

## ✅ **Checklist**

- [ ] Download all 6 WEEK_2_PHASE_*.md files
- [ ] Read Phase 1 file first (understand structure)
- [ ] Submit Phase 1 to orchestrator
- [ ] Wait for Phase 1 completion (~20 min)
- [ ] Submit Phase 2
- [ ] Wait and repeat for Phases 3-6
- [ ] At the end: Fully functional API + dashboard + vendor abstraction

---

## 🎯 **What You'll Have After Week 2**

✅ **REST API** (FastAPI)
- POST /api/upload (CSV)
- POST /api/inference (model predictions)
- GET /health (health checks)

✅ **Streamlit Dashboard**
- Sensor data visualization
- Anomaly timeline
- Threshold adjustment
- Performance metrics

✅ **Database**
- SensorReading table with multi-tenant support
- Alembic migrations (version control)
- Test data seeding

✅ **Vendor Abstraction**
- CSV adapter (Phase 1)
- Priva stub (Phase 1.5 ready)
- Easy to add more vendors

✅ **Testing**
- 26+ tests (health, API, integration, end-to-end)
- >85% code coverage
- Coverage report (HTML)

✅ **Documentation**
- Architecture design docs
- 3 ADRs (decisions documented)
- Deployment guide
- Code review report

---

## 💡 **Key Features**

1. **Multi-tenant** - Each customer sees only their data
2. **Vendor agnostic** - CSV now, Priva in Phase 1.5, others later
3. **Interactive** - Dashboard for exploring results
4. **Production-ready** - Tests, logging, error handling
5. **Well-documented** - ADRs, deployment guide, code review

---

## 📍 **Next After Week 2**

You have **two options**:

### **Option A: Continue to Phase 1.5 (Model Learning)**
- Upload real greenhouse data
- Model learns your baseline
- Upload new data → detects anomalies specific to YOUR greenhouse
- **This is when the magic happens!** ✨

### **Option B: Do Phase 2 (Threshold Tuning)**
- Refine dashboard
- Manual threshold adjustment
- Operator feedback loop
- Eventually leads to Phase 1.5 anyway

**Recommendation:** Skip Phase 2, go straight to Phase 1.5. You get real value sooner!

---

## 📁 **Files You Have**

**Expanded Week 2:**
- `WEEK_2_EXPANDED_FULL.md` (original complete document)

**Split into phases:**
- `WEEK_2_PHASE_1.md` ← Start here!
- `WEEK_2_PHASE_2.md`
- `WEEK_2_PHASE_3.md`
- `WEEK_2_PHASE_4.md`
- `WEEK_2_PHASE_5.md`
- `WEEK_2_PHASE_6.md`

**Plus context:**
- `DATA_LEARNING_TIMELINE.md` (when you can upload + learn)

---

## 🎬 **Ready to Start?**

1. Open `WEEK_2_PHASE_1.md`
2. Copy the "ORCHESTRATOR SUBMISSION TEXT" section
3. Paste to `claude` terminal at `❯` prompt
4. Press Enter
5. Wait ~20 minutes

Then repeat for Phase 2, 3, 4, 5, 6!

**Let's build! 🚀**

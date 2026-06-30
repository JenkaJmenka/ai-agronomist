# AI AGRONOMIST IMPLEMENTATION PROMPTS
## Master Index - All 6 Phases Ready for Claude Code

---

## 📚 HOW TO USE THESE PROMPTS

### Quick Start

1. **Open Claude Code** → Create new project
2. **Copy entire prompt** from one of the phase files below
3. **Paste into Claude Code** at the `❯` prompt
4. **Wait for orchestrator to complete** (generates code, tests, documentation)
5. **Move to next phase** when current completes

---

## 🗂️ ALL 6 PHASES

### ⏱️ PHASE 1: Foundation + Seasonal Patterns (1 week)
**File:** `/mnt/user-data/outputs/PHASE_1_ORCHESTRATOR_PROMPT.md`

**What it does:**
- FastAPI backend setup
- Seasonal pattern learning (4 patterns: spring/summer/autumn/winter)
- CSV parser (any format)
- Train endpoint
- 30+ tests

**Key deliverables:**
```
✓ API listening on :8000
✓ Database with 9 tables
✓ Pattern learning from data
✓ Train endpoint: POST /api/anomaly/train
```

**Estimated time:** 5-7 days

---

### 🎯 PHASE 2: Detectors + Active Learning (1 week)
**File:** `/mnt/user-data/outputs/PHASE_2_ORCHESTRATOR_PROMPT.md`

**What it does:**
- 5 univariate detectors (spike, stuck, drift, precision, polling)
- Detector voting aggregation (confidence scoring)
- Operator feedback collection
- Active learning loop (auto-retrain on 20+ corrections)
- Adaptive threshold optimization (grid search)
- 50+ tests

**Key deliverables:**
```
✓ All detectors independently tested
✓ Feedback API: POST /api/feedback/mark-anomaly
✓ Auto-retrain when threshold met
✓ Threshold optimization working
```

**Estimated time:** 5-7 days

---

### ✨ PHASE 3: Advanced Features (SHAP + Clustering) (1 week)
**File:** `/mnt/user-data/outputs/PHASE_3_ORCHESTRATOR_PROMPT.md`

**What it does:**
- SHAP explainability (why anomaly flagged)
- Anomaly clustering (group related anomalies into incidents)
- Multivariate detectors (Mahalanobis, GDN wrapper)
- Physics validator (thermodynamics checks)
- 40+ tests

**Key deliverables:**
```
✓ Explanations with top 5 contributing sensors
✓ Anomalies grouped into clusters with severity
✓ Recommendations generated automatically
✓ Physics validation flags impossible values
```

**Estimated time:** 5-7 days

---

### 🔍 PHASE 4: Pass 2 Detection Pipeline (1 week)
**File:** `/mnt/user-data/outputs/PHASE_4_ORCHESTRATOR_PROMPT.md`

**What it does:**
- Pass 2 detector: row-by-row anomaly detection
- Seasonal-aware detection (right thresholds for right season)
- Result aggregation (anomalies CSV export)
- Pattern management API (list, retrieve, reset)
- Detect endpoint (uses stored pattern)
- Analyze endpoint (train + detect all-in-one)
- 50+ tests

**Key deliverables:**
```
✓ Detect API: POST /api/anomaly/detect
✓ Analyze API: POST /api/anomaly/analyze
✓ Pattern management: GET /api/anomaly/patterns/{id}
✓ Anomalies CSV with explanations + clusters
```

**Estimated time:** 5-7 days

---

### 🧠 PHASE 5: Transfer Learning & Optimization (1 week)
**File:** `/mnt/user-data/outputs/PHASE_5_ORCHESTRATOR_PROMPT.md`

**What it does:**
- Transfer learning framework (share knowledge across customers)
- Model aggregation (combine learnings)
- Fine-tuning (adapt to new customer)
- Pattern caching (60-min TTL)
- Connection pooling (DB + Redis)
- Performance optimization (1000+ rows/sec)
- 30+ tests + performance benchmarks

**Key deliverables:**
```
✓ Shared knowledge extraction working
✓ New customer converges 4x faster (7 days vs 30)
✓ Detection throughput >1000 rows/sec
✓ Pattern cache reduces DB load
```

**Estimated time:** 5-7 days

---

### 🚀 PHASE 6: Production Hardening & Deployment (1 week)
**File:** `/mnt/user-data/outputs/PHASE_6_ORCHESTRATOR_PROMPT.md`

**What it does:**
- JWT authentication
- Rate limiting (60 req/min)
- Global error handling
- Structured logging (JSON)
- Input validation (injection prevention)
- Docker containerization
- Full deployment automation
- Monitoring setup
- Complete documentation (API + deployment + monitoring)
- Security tests
- 20+ security + integration tests

**Key deliverables:**
```
✓ Docker image builds
✓ docker-compose up starts all services
✓ Health check passes
✓ All 200+ tests pass
✓ API docs at /docs
✓ Ready for production
```

**Estimated time:** 5-7 days

---

## 📊 TIMELINE SUMMARY

| Phase | Duration | Status | Focus |
|-------|----------|--------|-------|
| 1 | 1 week | Ready | Foundation + Seasonal |
| 2 | 1 week | Ready | Detectors + Learning |
| 3 | 1 week | Ready | Explainability + Clustering |
| 4 | 1 week | Ready | Detection Pipeline |
| 5 | 1 week | Ready | Transfer Learning |
| 6 | 1 week | Ready | Production Ready |
| **TOTAL** | **6 weeks** | **READY** | **GO LIVE** |

---

## 🎯 OPERATING MODES AT LAUNCH (Phase 4+)

### Mode 1: Upload & Train
```
POST /api/anomaly/train
├─ Input: CSV
├─ Process: Learn 4 seasonal patterns
└─ Output: Pattern profiles (IDs + thresholds)
```

### Mode 2: Upload & Detect
```
POST /api/anomaly/detect
├─ Input: CSV + pattern_id
├─ Process: Row-by-row detection with seasonal thresholds
├─ SHAP explanations
├─ Anomaly clustering
└─ Output: Anomalies CSV (explained + clustered)
```

### Mode 3: Upload & Analyze
```
POST /api/anomaly/analyze
├─ Input: CSV (no pattern needed)
├─ Process: Train (seasonal) → Detect → Explain → Cluster
└─ Output: Patterns + Anomalies (full analysis)
```

---

## 💾 EXPECTED FILE STRUCTURE AFTER ALL PHASES

```
ai-agronomist/
├── api/
│   ├── main.py
│   ├── routes/
│   │   ├── anomaly.py (train/detect/analyze)
│   │   ├── patterns.py (management)
│   │   └── feedback.py (operator corrections)
│   └── middleware/
│       ├── auth.py (JWT)
│       ├── rate_limiter.py (60 req/min)
│       ├── error_handler.py (global)
│       └── cache_middleware.py (pattern caching)
├── core/
│   └── config.py
├── database/
│   ├── models.py (9 tables)
│   ├── crud.py
│   └── session.py (connection pooling)
├── detectors/
│   ├── base.py
│   ├── spike.py
│   ├── stuck.py
│   ├── drift.py
│   ├── precision.py
│   ├── polling.py
│   ├── multivariate.py
│   ├── lstm_ae_wrapper.py
│   └── physics_validator.py
├── engines/
│   ├── pass1_learn.py
│   ├── pass1_seasonal_splitter.py
│   ├── pass2_detect.py
│   ├── pass2_detect_seasonal.py
│   ├── pass2_result_aggregator.py
│   ├── voting.py
│   ├── anomaly_clustering.py
│   ├── threshold_optimizer.py
│   ├── model_aggregation.py
│   ├── fine_tuning.py
│   └── root_cause_inference.py
├── models/
│   ├── anomaly.py
│   ├── explainability.py
│   ├── active_learner.py
│   └── transfer_learning.py
├── parsers/
│   └── csv_parser.py
├── storage/
│   ├── model_store.py
│   └── result_store.py
├── utils/
│   ├── logger.py
│   ├── validators.py
│   └── cache.py
├── tests/
│   ├── unit/ (150+ tests)
│   ├── integration/ (30+ tests)
│   ├── performance/ (10+ tests)
│   └── security/ (10+ tests)
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── .env.example
├── config/
│   └── logging.yaml
├── docs/
│   ├── API.md
│   ├── DEPLOYMENT.md
│   └── MONITORING.md
├── scripts/
│   └── deploy.sh
├── requirements.txt
├── .gitignore
└── README.md
```

---

## 🧪 TEST COVERAGE

| Phase | Unit Tests | Integration Tests | Total |
|-------|-----------|------------------|-------|
| 1 | 20+ | 5+ | 25+ |
| 2 | 30+ | 5+ | 35+ |
| 3 | 25+ | 0 | 25+ |
| 4 | 10+ | 15+ | 25+ |
| 5 | 20+ | 5+ | 25+ |
| 6 | 15+ | 5+ | 20+ |
| **TOTAL** | **120+** | **35+** | **200+** |

**Expected coverage:** 80%+

---

## 🚀 EXECUTION WORKFLOW

### Step-by-Step

1. **Create Claude Code project**
   ```bash
   # In Claude Code terminal
   cd ~/Projects && mkdir ai-agronomist && cd ai-agronomist
   ```

2. **Paste Phase 1 prompt**
   - Copy full text from `PHASE_1_ORCHESTRATOR_PROMPT.md`
   - Paste at `❯` prompt in Claude Code
   - Wait for completion (5-7 days virtual time, <1 day real time)
   - Check: `pytest tests/ -v` passes with 25+/25+ passing

3. **Paste Phase 2 prompt**
   - Copy from `PHASE_2_ORCHESTRATOR_PROMPT.md`
   - Paste at `❯` prompt
   - Wait for completion
   - Check: 35+/35+ tests passing

4. **Repeat for Phases 3-6**
   - Each phase builds on previous
   - Each adds new capabilities
   - Tests accumulate (200+ total)

5. **Go Live**
   - After Phase 6 completes
   - `bash scripts/deploy.sh` to deploy
   - Onboard first pilot customer
   - Monitor metrics

---

## ⚠️ IMPORTANT NOTES

### Prompt Size
Each prompt is **8,000-15,000 lines** of instructions + code. Claude Code handles this well with the multi-agent orchestrator architecture.

### Auto-Proceed
Each phase ends with `**auto-proceed**` flag. This tells the orchestrator to automatically move to next phase when current completes.

### Database
- Phase 1 initializes SQLite (dev) or PostgreSQL (prod)
- Schemas auto-created via SQLAlchemy
- Data persists across phases

### No Live Data Required
- All phases work with synthetic/historical data
- Priva API integration deferred to Phase 1.5 (backlog)
- No live customer needed until Phase 6 complete

### Git Integration
- Each phase generates production code (not experiments)
- All code is ready for git commit
- `.gitignore` handles models/ and results/ directories

---

## 📖 READING ORDER

**If you want to understand implementation before running:**

1. Read: `IMPLEMENTATION_PLAN.md` (overview)
2. Read: Phase 1 prompt (foundation)
3. Then run: Phase 1 prompt in Claude Code

**If you want to run immediately:**

Just start pasting prompts. System is designed to be self-explanatory.

---

## 🔄 ITERATION & FEEDBACK

**During each phase:**
- Orchestrator generates code + tests
- Tests validate implementation
- If tests fail: orchestrator fixes automatically
- Success criteria clearly defined

**After all 6 phases:**
- You have production-ready system
- Ready to deploy
- Ready to collect pilot feedback
- Ready to iterate

---

## 📞 QUICK REFERENCE

| Need | Where | Command |
|------|-------|---------|
| Phase 1 prompt | File | `/mnt/user-data/outputs/PHASE_1_ORCHESTRATOR_PROMPT.md` |
| Phase 2 prompt | File | `/mnt/user-data/outputs/PHASE_2_ORCHESTRATOR_PROMPT.md` |
| Phase 3 prompt | File | `/mnt/user-data/outputs/PHASE_3_ORCHESTRATOR_PROMPT.md` |
| Phase 4 prompt | File | `/mnt/user-data/outputs/PHASE_4_ORCHESTRATOR_PROMPT.md` |
| Phase 5 prompt | File | `/mnt/user-data/outputs/PHASE_5_ORCHESTRATOR_PROMPT.md` |
| Phase 6 prompt | File | `/mnt/user-data/outputs/PHASE_6_ORCHESTRATOR_PROMPT.md` |
| Implementation plan | File | `/mnt/user-data/outputs/IMPLEMENTATION_PLAN.md` |
| Run tests | Terminal | `pytest tests/ -v` |
| View docs | Browser | `http://localhost:8000/docs` |
| Deploy | Script | `bash scripts/deploy.sh` |

---

## ✅ CHECKLIST: READY TO START

- [ ] Review `IMPLEMENTATION_PLAN.md`
- [ ] Have Claude Code open
- [ ] Have terminal ready (`~/Projects/ai-agronomist/`)
- [ ] Paste Phase 1 prompt
- [ ] Wait for completion
- [ ] Check tests pass
- [ ] Repeat for Phases 2-6
- [ ] Deploy to production
- [ ] Onboard first customer
- [ ] Monitor in production

---

**🎯 Goal:** Production-ready anomaly detection system in 6 weeks

**📈 Outcome:** Onboard first pilot customer → collect feedback → iterate

**🚀 Status:** All phases ready. Start whenever you're ready.

---

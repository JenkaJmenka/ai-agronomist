# Phase 1.5 - WEEK 6: Priva Integration & Production Hardening
## Automated Data Pipeline + Production Deployment (Final, 25 min)

---

## 📋 **Week 6 Overview**

**Final phase** — runs AFTER Week 5 completes  
**Duration:** ~25 minutes  
**Goal:** Implement Priva API integration, automated training pipeline, production hardening

**Dependencies:**
- ✅ Week 5 complete (A/B testing working)

---

## 🚀 **Submit This to Orchestrator**

Wait for Week 5 to complete, then paste:

---

## **ORCHESTRATOR SUBMISSION TEXT**

```
Complete Phase 1.5 Week 6 - Priva integration, automated training, and production hardening.

Phase 1.5.6 (after Phase 1.5.5):

- architect: Design automated training pipeline and production architecture.
  Output: docs/architecture/priva_integration_design.md, docs/architecture/training_pipeline_design.md
  
  Deliverables:
  
  1. docs/architecture/priva_integration_design.md
     Design document:
     - Priva API credentials (OAuth 2.0)
     - Data polling strategy (every 15/30 min)
     - Rate limiting (Priva API limits)
     - Error handling (network failures, timeouts)
     - Data validation (Priva format → normalized format)
     - Incremental updates (only fetch new data since last poll)
  
  2. docs/architecture/training_pipeline_design.md
     Design document:
     - Automated scheduler (cron-like)
     - Trigger conditions:
       * Weekly: re-train on past week's data
       * On feedback threshold: re-train after 50 feedback marks
       * On demand: operator can trigger
     - Training notifications (email/dashboard when complete)
     - Failed training handling (retry, alert)
     - Model versioning (auto-increment version)

- ml-engineer: Implement Priva API client (real, not stub).
  Output: src/vendors/priva_client.py (updated), src/models/priva_connector.py
  
  Deliverables:
  
  1. src/vendors/priva_client.py (UPDATED from Week 2 stub)
     class PrivaClient(DataSourceAdapter):
       
       def __init__(self, api_key: str, api_secret: str, 
                   base_url: str = "https://api.priva.com"):
         """Initialize with OAuth credentials"""
       
       async def authenticate(self) -> str:
         """
         Perform OAuth 2.0 authentication.
         
         Process:
         1. POST /oauth/authorize with api_key + api_secret
         2. Receive access_token
         3. Store token + expiration
         4. Return token
         
         Note: Implement token refresh before expiration
         """
       
       async def fetch_data(self, 
                           customer_id: str,
                           greenhouse_id: str,
                           start: datetime,
                           end: datetime) -> np.ndarray:
         """
         Fetch sensor data from Priva API.
         
         Process:
         1. Call GET /api/v1/greenhouse/{greenhouse_id}/sensors
         2. Filter by timestamp range
         3. Parse response (Priva JSON format)
         4. Convert to standard format: (N, 4) with [temp, humidity, co2, light]
         5. Return normalized [0, 1]
         
         Priva response format (expected):
         {
           "sensors": [
             {"id": "temp_1", "name": "Temperature", "readings": [
               {"timestamp": "2026-06-01T00:00:00Z", "value": 23.5},
               ...
             ]},
             ...
           ]
         }
         """
       
       async def validate_data(self, data: np.ndarray) -> Tuple[bool, str]:
         """Validate Priva data format"""
       
       async def normalize(self, data: np.ndarray) -> np.ndarray:
         """Normalize Priva sensor ranges to [0, 1]"""
       
       @property
       def name(self) -> str:
         return "priva"
       
       async def _refresh_token_if_needed(self):
         """Check expiration, refresh if needed"""
       
       async def _map_priva_to_standard(self, priva_response: Dict) -> np.ndarray:
         """Convert Priva JSON → standard numpy array"""
  
  2. src/models/priva_connector.py
     class PrivaConnector:
       
       def __init__(self, priva_client: PrivaClient):
         """Initialize with Priva client"""
       
       async def poll_latest_data(self, 
                                 customer_id: str,
                                 greenhouse_id: str,
                                 limit_hours: int = 24) -> np.ndarray:
         """
         Poll latest data from Priva.
         
         Args:
         - limit_hours: fetch last N hours (default 24)
         
         Returns: latest sequences from Priva
         """
       
       async def store_priva_data(self,
                                 customer_id: str,
                                 greenhouse_id: str,
                                 data: np.ndarray):
         """
         Store Priva data in local database.
         
         Process:
         1. Insert into SensorReading table
         2. Track: data from Priva (source="priva")
         3. Available for training
         """

- backend-engineer: Implement automated training scheduler.
  Output: src/models/training_scheduler.py, src/api/routes/training.py
  
  Deliverables:
  
  1. src/models/training_scheduler.py
     class TrainingScheduler:
       
       def __init__(self, db_session, training_pipeline):
         """Initialize scheduler"""
       
       async def schedule_daily_training(self):
         """
         Run daily at midnight (configurable).
         
         For each customer:
         1. Check if new data available (since last training)
         2. If yes: trigger training
         3. Evaluate: is new model better?
         4. If better: deploy
         5. If worse: keep previous model
         """
       
       async def schedule_feedback_triggered_training(self):
         """
         Trigger training after 50 feedback marks.
         
         Rationale: feedback indicates threshold needs adjustment
         Re-train to improve on operator feedback
         """
       
       async def monitor_training_jobs(self):
         """
         Monitor running training jobs.
         
         Check status every 5 minutes:
         - Still running? OK
         - Completed? Mark as done, notify operator
         - Failed? Retry or alert
         """
       
       async def send_notifications(self,
                                   customer_id: str,
                                   event: str,
                                   details: Dict):
         """
         Send notifications on training events.
         
         Events:
         - "training_started": Training begun for customer
         - "training_completed": New model ready
         - "training_failed": Error during training (retry or alert)
         - "model_deployed": New model deployed to production
         
         Methods: email, dashboard notification, Slack (future)
         """
  
  2. src/api/routes/training.py
     New endpoints:
     
     - GET /api/training/status/{customer_id}
       Get training status
       Response: {
         "customer_id": "...",
         "last_training": "2026-06-08T12:00:00Z",
         "status": "completed",
         "model_id": "model_v3",
         "next_scheduled": "2026-06-09T00:00:00Z"
       }
     
     - POST /api/training/trigger/{customer_id}
       Manually trigger training
       Response: {
         "training_id": "training_123",
         "status": "started",
         "eta_minutes": 45
       }
     
     - GET /api/training/history/{customer_id}
       Get training history
       Response: [{training record}, ...]

- backend-engineer: Update dashboard with Priva status.
  Output: src/dashboard/pages/data_pipeline.py
  
  Deliverable:
  
  New Streamlit page: src/dashboard/pages/data_pipeline.py
  
  Functionality:
  
  1. Priva Connection Status
     - Connected: green ✅
     - Disconnected: red ❌
     - Last successful poll: timestamp
     - Next poll: estimated time
  
  2. Data Flow
     - "Data source: Priva IoT sensors"
     - "Polling interval: every 30 minutes"
     - "Latest data: 2 hours ago"
     - "Total sequences stored: 10,000+"
  
  3. Training Pipeline Status
     - Last training: "2026-06-08 12:00 (3 days ago)"
     - Current model: "model_v3 (improvement: +28%)"
     - Next training: "scheduled for 2026-06-10 00:00"
     - Training history: list of past trainings
  
  4. Data Quality
     - Missing values: 0
     - Out of range: 5 (< 0.1%)
     - Data freshness: "current (< 1 hour old)"
  
  5. Alerts
     - "Priva API unreachable (retry in 5 min)"
     - "Training queue: 2 jobs pending"
     - "No data for 6 hours (check connection)"

- devops-lead: Production hardening and monitoring.
  Output: src/logging/structured_logging.py (updated), src/models/health_checks.py, docker-compose.yml (updated)
  
  Deliverables:
  
  1. Production logging configuration
     - Structured JSON logging (not pretty-printed)
     - Fields: timestamp, level, service, request_id, duration, customer_id
     - Sensitive field redaction (hide API keys)
     - Log rotation (daily, keep 30 days)
     - Example log:
       {
         "timestamp": "2026-06-08T12:30:45Z",
         "level": "INFO",
         "service": "training",
         "customer_id": "customer_tomato_1",
         "event": "training_completed",
         "duration_seconds": 1800,
         "model_id": "model_v3",
         "improvement_percent": 28
       }
  
  2. Health checks
     - /health: basic health
     - /health/db: database connectivity
     - /health/priva: Priva API reachable
     - /health/models: model checkpoints accessible
     - /health/training: training scheduler running
  
  3. Monitoring & Alerting
     - Prometheus metrics:
       * inference_latency_ms (histogram)
       * anomalies_detected_count (counter)
       * training_job_duration_seconds (gauge)
       * priva_poll_success_rate (percentage)
     
     - Alerts:
       * Inference latency > 500ms (slow!)
       * Priva API error rate > 10%
       * Training job failed
       * Model checkpoint missing
  
  4. Docker improvements
     - Health check: HEALTHCHECK instruction
     - Resource limits: memory, CPU
     - Volume mounts: persistent storage for models
     - Environment variables: secrets from .env

- qa-engineer: Test Priva integration and training pipeline.
  Output: tests/vendors/test_priva_client.py, tests/models/test_training_scheduler.py, tests/integration/test_full_pipeline.py
  
  Deliverables:
  
  1. tests/vendors/test_priva_client.py
     Test cases:
     - test_priva_authenticate: OAuth flow works
     - test_priva_fetch_data: Data fetched from API
     - test_priva_data_format: Correct shape (N, 4)
     - test_priva_normalization: Data normalized [0, 1]
     - test_priva_error_handling: Timeouts, 401, 403 handled
     - test_priva_token_refresh: Token refreshed before expiration
     (Note: use mock Priva API for testing)
  
  2. tests/models/test_training_scheduler.py
     Test cases:
     - test_daily_training_trigger: Runs at midnight
     - test_feedback_triggered_training: Runs after 50 feedback marks
     - test_training_notification: Sends notification on completion
     - test_model_deployment: New model deployed if better
     - test_training_job_monitoring: Status tracked correctly
     - test_failed_training_retry: Failed jobs retried
  
  3. tests/integration/test_full_pipeline.py
     End-to-end pipeline test:
     - Priva polls data
     - Data stored in database
     - Training triggered
     - New model trained
     - A/B test on new model
     - Model deployed
     - Inference uses new model
  
  4. Performance tests:
     - Inference latency < 500ms
     - Training time < 2 hours
     - Priva poll < 10 seconds

- code-reviewer: Review Phase 1.5 and create documentation.
  Output: docs/review/phase_1_5_review.md, docs/PRODUCTION_DEPLOYMENT.md, docs/architecture/ADR-007-phase-1-5.md
  
  Deliverables:
  
  1. docs/review/phase_1_5_review.md
     Code review covering:
     - Baseline analysis implementation
     - Transfer learning correctness
     - Per-customer model isolation
     - Seasonal model logic
     - Feedback aggregation
     - A/B testing framework
     - Priva integration
     - Overall: PASS / NEEDS FIXES
  
  2. docs/PRODUCTION_DEPLOYMENT.md
     Deployment guide:
     - Prerequisites (Priva credentials, etc.)
     - Environment variables (.env setup)
     - Database migration
     - Model checkpoint setup
     - Scheduler configuration
     - Monitoring setup
     - Runbook: troubleshooting common issues
  
  3. docs/architecture/ADR-007-phase-1-5.md
     Architecture decision record:
     - Why transfer learning?
     - Why per-customer models?
     - Why seasonal variants?
     - Why A/B testing before deployment?
     - Future improvements

---

## ✅ **Week 6 Success Criteria**

- [ ] `src/vendors/priva_client.py` real implementation (not stub)
- [ ] `src/models/priva_connector.py` polling Priva successfully
- [ ] `src/models/training_scheduler.py` runs daily training
- [ ] Can monitor training job status via API
- [ ] Dashboard shows Priva status + data pipeline
- [ ] Health checks include Priva, database, models
- [ ] Structured JSON logging implemented
- [ ] Prometheus metrics exported
- [ ] Production Docker setup with health checks
- [ ] `tests/vendors/test_priva_client.py` passes (6 tests, mocked Priva)
- [ ] `tests/models/test_training_scheduler.py` passes (6 tests)
- [ ] `tests/integration/test_full_pipeline.py` passes (1 test)
- [ ] Code review completed
- [ ] Production deployment guide complete
- [ ] ADR-007 filed

---

## 🧪 **Test Commands** (After Week 6)

```bash
# Test Priva client (mocked)
pytest tests/vendors/test_priva_client.py -v

# Test training scheduler
pytest tests/models/test_training_scheduler.py -v

# Test full pipeline
pytest tests/integration/test_full_pipeline.py -v -s

# Check health endpoints
curl http://localhost:8000/health/priva
# Expected: {"status": "ok"} or {"status": "error"}

# Check training status
curl http://localhost:8000/api/training/status/customer_tomato_1
# Expected: {"status": "completed", "model_id": "model_v3", ...}
```

---

## 📊 **Production Architecture**

```
Priva IoT Greenhouse Sensors
    ↓ (HTTP REST API)
Priva Cloud
    ↓ (OAuth 2.0 authenticated)
PrivaConnector (polls every 30 min)
    ↓
AI Agronomist API (FastAPI)
    ├─ Stores in PostgreSQL
    └─ Queues training job
        ↓
TrainingScheduler (runs daily @ midnight)
    ├─ Checks new data available
    ├─ Triggers PerCustomerTrainingPipeline
    ├─ Trains seasonal models
    ├─ A/B tests new model
    └─ Deploys if better
        ↓
ModelRegistry (version control)
    ├─ Store model checkpoints
    ├─ Track all versions
    └─ Enable rollback
        ↓
Inference Engine
    ├─ Load customer model
    ├─ Detect season
    ├─ Predict anomalies
    └─ Record feedback
        ↓
Dashboard
    ├─ Visualize results
    ├─ Operator marks false positives
    └─ Track model performance
```

---

## 💡 **Key Production Points**

- **Automated Data:** Priva replaces manual CSV uploads
- **Automated Training:** Daily models, no operator intervention
- **Safety First:** A/B testing before deployment
- **Observable:** Structured logging, Prometheus metrics
- **Recoverable:** Health checks, rollback capability

---

## 📍 **Phase 1.5 Complete!**

When Week 6 completes, you have:

✅ **Baseline Analysis** - Understands customer data patterns  
✅ **Transfer Learning** - Adapts Week 1 model to customers  
✅ **Per-Customer Models** - Each customer gets custom model  
✅ **Seasonal Variants** - Summer/winter models  
✅ **Feedback Loop** - Operator marks false positives  
✅ **Auto-Tuning** - Threshold adjusts based on feedback  
✅ **A/B Testing** - Safe deployment of new models  
✅ **Priva Integration** - Automated data from sensors  
✅ **Production Ready** - Logging, monitoring, health checks  

---

## 🚀 **What Comes After Phase 1.5**

### **Option A: Phase 2 (Threshold Tuning)**
- More advanced threshold strategies
- Ensemble methods
- ~4 weeks

### **Option B: Phase 3 (Multi-Vendor)**
- Hoogendoorn API
- Custom IoT
- ~4 weeks

### **Option C: Phase 4 (Autonomous Control)**
- AI recommends actions
- Operator approval
- Emergency protocols
- ~8 weeks

---

**Ready?** Wait for Week 5 to finish, then execute final week! 🚀

After Phase 1.5 complete: **You have a production-ready per-customer anomaly detector!** ✨

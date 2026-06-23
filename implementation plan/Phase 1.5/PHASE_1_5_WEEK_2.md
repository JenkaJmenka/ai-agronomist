# Phase 1.5 - WEEK 2: Per-Customer Models & Registry
## Model Versioning & Operator Feedback UI (Sequential, 20 min)

---

## 📋 **Week 2 Overview**

**Sequential** — runs AFTER Week 1 completes  
**Duration:** ~20 minutes  
**Goal:** Create model registry, per-customer training pipeline, feedback UI

**Dependencies:**
- ✅ Week 1 complete (Baseline analysis + transfer learning working)

---

## 🚀 **Submit This to Orchestrator**

Wait for Week 1 to complete, then paste:

---

## **ORCHESTRATOR SUBMISSION TEXT**

```
Continue Phase 1.5 Week 2 - Model registry, per-customer training, and feedback UI.

Phase 1.5.2 (after Phase 1.5.1):

- backend-engineer: Implement model registry and per-customer training pipeline.
  Output: src/models/model_registry.py, src/db/migrations/002_add_model_tables.py, src/api/routes/models.py, src/models/training_pipeline.py
  
  Deliverables:
  
  1. src/models/model_registry.py
     class ModelRegistry:
       
       def __init__(self, db_session):
         """Initialize with database connection"""
       
       async def register_model(self,
                               customer_id: str,
                               model_type: str,  # "transfer_learned" or "baseline"
                               checkpoint_path: str,
                               baseline_id: str,
                               metrics: Dict) -> str:
         """
         Register new model version.
         
         Returns: model_id (unique identifier)
         
         Stores:
         - customer_id
         - model_type (what kind of model)
         - checkpoint_path (where weights are saved)
         - baseline_id (which baseline it was trained on)
         - metrics (accuracy, mse, etc.)
         - created_at (timestamp)
         - status ("active", "archived")
         """
       
       async def get_active_model(self, customer_id: str) -> Dict:
         """Get currently deployed model for customer"""
       
       async def list_models(self, customer_id: str) -> List[Dict]:
         """List all model versions for customer"""
       
       async def rollback_model(self, customer_id: str, model_id: str):
         """Rollback to previous model version"""
       
       async def archive_model(self, model_id: str):
         """Mark model as archived (historical)"""
  
  2. src/db/migrations/002_add_model_tables.py
     Create tables:
     
     - model_baseline
       id, customer_id, greenhouse_type, baseline_json, created_at
       Stores baseline profiles from Week 1
     
     - model_version
       id, customer_id, model_type, checkpoint_path, baseline_id,
       metrics_json, created_at, status (active/archived)
       Tracks all model versions per customer
     
     - model_deployment
       id, customer_id, model_id, deployed_at, is_current
       Tracks which model is deployed when
     
     - operator_feedback
       id, customer_id, sequence_id, is_false_positive, feedback_at
       Stores operator marks (false positive, etc.)
  
  3. src/api/routes/models.py
     New endpoints:
     
     - GET /api/models/{customer_id}
       Returns: active model for customer
       Response: {
         "model_id": "...",
         "customer_id": "...",
         "created_at": "2026-06-08T...",
         "metrics": {"mse": 0.042, "improvement": "23%"}
       }
     
     - GET /api/models/{customer_id}/versions
       Returns: all model versions (active + archived)
       Response: [{...}, {...}, ...]
     
     - POST /api/models/{customer_id}/train
       Trigger training for customer
       Request: {"baseline_id": "..."}
       Response: {
         "training_id": "...",
         "status": "started",
         "eta_minutes": 45
       }
       Note: Training runs async, check status with GET /api/models/{customer_id}/training/{training_id}
     
     - POST /api/models/{customer_id}/rollback
       Rollback to previous model
       Request: {"model_id": "..."}
       Response: {"status": "success", "model_id": "..."}
  
  4. src/models/training_pipeline.py
     class PerCustomerTrainingPipeline:
       
       async def train_customer_model(self,
                                     customer_id: str,
                                     baseline_id: str) -> str:
         """
         End-to-end training for customer.
         
         Steps:
         1. Load customer's baseline from database
         2. Get customer's data (stored sequences)
         3. Run transfer learning (fine-tune Week 1 model)
         4. Evaluate new model
         5. Compare to previous model
         6. If better: register as new version
         7. Return model_id
         
         Training time: ~30-60 min depending on data size
         """
       
       async def _load_customer_data(self, customer_id: str) -> np.ndarray:
         """Load all of customer's sequences from database"""
       
       async def _run_transfer_learning(self,
                                       customer_data: np.ndarray,
                                       baseline_id: str) -> Tuple[LSTMVAE, Dict]:
         """Run fine-tuning, return model + metrics"""
       
       async def _compare_to_previous(self,
                                     customer_id: str,
                                     new_metrics: Dict) -> Dict:
         """Compare to previously deployed model"""
       
       async def _register_if_better(self,
                                    customer_id: str,
                                    model: LSTMVAE,
                                    metrics: Dict,
                                    is_improvement: bool) -> str:
         """Register model if it improves accuracy"""

- backend-engineer: Update API to integrate model registry.
  Output: src/api/main.py (updated), tests/api/test_models.py
  
  Deliverables:
  
  1. src/api/main.py (UPDATED)
     Add:
     - Model routes import: from src.api.routes import models
     - Register routes: app.include_router(models.router)
     - Global dependency: inject ModelRegistry into routes
     - Load active model on startup (for inference)
  
  2. tests/api/test_models.py
     Test cases:
     - test_get_active_model: GET /api/models/{customer_id}
     - test_list_model_versions: GET /api/models/{customer_id}/versions
     - test_train_customer_model: POST /api/models/{customer_id}/train
     - test_rollback_model: POST /api/models/{customer_id}/rollback
     - test_model_registration: New model registered in database
     - test_model_versioning: Multiple versions tracked per customer

- backend-engineer: Update inference to use per-customer models.
  Output: src/api/routes/inference.py (updated), src/models/inference.py (updated)
  
  Deliverables:
  
  1. src/api/routes/inference.py (UPDATED)
     Modify POST /api/inference to:
     - Accept customer_id in request body
     - Load customer's active model (not Week 1 baseline)
     - Run inference with customer model
     - Example request:
       {
         "customer_id": "customer_tomato_1",
         "greenhouse_id": "gh1",
         "sequences": [...]
       }
     - Response now includes:
       {
         "model_id": "model_v3",
         "customer_id": "customer_tomato_1",
         "sequences_processed": 150,
         "anomalies_detected": 5,
         "anomaly_scores": [...]
       }
  
  2. src/models/inference.py (UPDATED)
     Enhance AnomalyDetector to support multiple models:
     - __init__(self, checkpoint_path: str, customer_id: str = None)
     - load_checkpoint() - smart loading (Week 1 baseline or customer-specific)
     - get_model_info() - return which model is loaded
     - Maintain in-memory cache of multiple customer models

- qa-engineer: Test per-customer training pipeline.
  Output: tests/models/test_training_pipeline.py, tests/integration/test_training_end_to_end.py
  
  Deliverables:
  
  1. tests/models/test_training_pipeline.py
     Test cases:
     - test_load_customer_data: Load customer data from database
     - test_train_customer_model_completes: Training runs and returns model_id
     - test_training_improves_accuracy: Customer model better than Week 1
     - test_compare_to_previous: Correctly compares to previous model
     - test_register_if_better: Registers only if improved
     - test_multiple_customers_independent: Training customer A doesn't affect B
  
  2. tests/integration/test_training_end_to_end.py
     Integration test:
     - Upload customer data (CSV)
     - Create baseline (Week 1 task)
     - Trigger training for customer
     - Verify model registered
     - Run inference with new model
     - Verify improved results
     - Rollback to Week 1 model
     - Verify inference uses old model

---

## ✅ **Week 2 Success Criteria**

- [ ] `src/models/model_registry.py` implements full registry
- [ ] `src/db/migrations/002_add_model_tables.py` creates 4 tables
- [ ] `src/api/routes/models.py` implements all 4 endpoints
- [ ] `src/models/training_pipeline.py` runs end-to-end training
- [ ] `src/api/routes/inference.py` updated to use per-customer models
- [ ] `src/models/inference.py` can load multiple customer models
- [ ] Per-customer models can be trained and registered
- [ ] Can rollback to previous model versions
- [ ] `tests/api/test_models.py` passes (6 tests)
- [ ] `tests/models/test_training_pipeline.py` passes (6 tests)
- [ ] `tests/integration/test_training_end_to_end.py` passes (1 integration test)

---

## 🧪 **Test Commands** (After Week 2)

```bash
# Run model registry tests
pytest tests/api/test_models.py -v

# Run training pipeline tests
pytest tests/models/test_training_pipeline.py -v

# Run end-to-end test (may take 1-2 minutes for training)
pytest tests/integration/test_training_end_to_end.py -v -s

# Check database tables created
sqlite3 app.db ".schema model_baseline"
# Should show: id, customer_id, greenhouse_type, baseline_json, created_at
```

---

## 📊 **Database Schema**

```sql
CREATE TABLE model_baseline (
  id VARCHAR PRIMARY KEY,
  customer_id VARCHAR NOT NULL,
  greenhouse_type VARCHAR,
  baseline_json TEXT NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE model_version (
  id VARCHAR PRIMARY KEY,
  customer_id VARCHAR NOT NULL,
  model_type VARCHAR NOT NULL,
  checkpoint_path VARCHAR NOT NULL,
  baseline_id VARCHAR,
  metrics_json TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  status VARCHAR DEFAULT 'active',
  FOREIGN KEY (baseline_id) REFERENCES model_baseline(id)
);

CREATE TABLE model_deployment (
  id VARCHAR PRIMARY KEY,
  customer_id VARCHAR NOT NULL,
  model_id VARCHAR NOT NULL,
  deployed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  is_current BOOLEAN DEFAULT FALSE,
  FOREIGN KEY (model_id) REFERENCES model_version(id)
);

CREATE TABLE operator_feedback (
  id VARCHAR PRIMARY KEY,
  customer_id VARCHAR NOT NULL,
  sequence_id VARCHAR,
  is_false_positive BOOLEAN,
  feedback_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 💡 **Key Design Points**

- **Model Registry:** Track all versions, enable rollback
- **Per-Customer Models:** Each customer gets adapted model
- **Training Pipeline:** Async training, can monitor progress
- **Inference:** Uses active model per customer
- **Operator Feedback:** Foundation for Week 4 feedback loop

---

## 📍 **What Happens Next**

When Week 2 completes:
1. ✅ Per-customer models working
2. ✅ Can train separate models for different customers
3. ✅ Can switch between models (rollback support)
4. ✅ Inference uses customer-specific model

Next: **Week 3** (Seasonal models - summer/winter variants)

---

**Ready?** Wait for Week 1 to finish! 🚀

# AI Agronomist Phase 1 — Agent Team Reference Guide

**Status:** Ready for implementation  
**Date:** May 13, 2026  
**Team Size:** 4 core members + 1 Solution Architect oversight

---

## Table of Contents
1. [Agent Personas & Responsibilities](#agent-personas)
2. [Guardrails & Constraints](#guardrails)
3. [Coding Standards](#coding-standards)
4. [Decision Workflows](#decision-workflows)
5. [Architecture Decision Records (ADR)](#adr)
6. [Weekly Sync & Review Checklist](#weekly-sync)
7. [Escalation & Red Flags](#red-flags)

---

# Agent Personas & Responsibilities

## 1. ML Engineer

### Profile
- **Title:** Machine Learning Engineer
- **Focus:** Model architecture, training, validation, metrics
- **Reports to:** Solution Architect (design oversight)
- **Dependencies:** Backend Engineer (data pipeline), DevOps (compute resources)

### Core Responsibilities
- [ ] Design LSTM-VAE architecture (encoder-decoder + latent space)
- [ ] Generate synthetic baseline data (TimeGAN or similar)
- [ ] Train model on synthetic + Phase 1 customer data
- [ ] Implement Isolation Forest as secondary anomaly detector
- [ ] Build correlation engine (physical ratio monitoring)
- [ ] Define performance metrics (reconstruction error, precision, recall)
- [ ] Create model registry (checkpoints + metadata)
- [ ] Validate model generalization (avoid overfitting to single customer)
- [ ] Document all hyperparameter choices
- [ ] Prepare transfer learning pipeline for Phase 1.5

### Expected Output (Phase 1)
```
artifacts/
├── models/
│   ├── lstm_vae_v1.0.pth          (model checkpoint)
│   ├── isolation_forest_v1.0.pkl  (ensemble baseline)
│   └── model_metadata.json        (training params, metrics)
├── notebooks/
│   ├── synthetic_data_generation.ipynb
│   ├── model_training.ipynb
│   ├── evaluation_metrics.ipynb
│   └── baseline_analysis.ipynb
├── src/models/
│   ├── lstm_vae.py               (PyTorch model)
│   ├── isolation_forest.py       (scikit-learn wrapper)
│   ├── correlation_engine.py     (physical ratio rules)
│   └── test_models.py            (unit tests)
└── docs/
    ├── model_architecture.md
    ├── synthetic_data_assumptions.md
    └── evaluation_report.md
```

### Definition of Done (ML Code)
- ✅ Model checkpoint saved with reproducible random seed
- ✅ Training notebook with loss curves + validation plots
- ✅ Reconstruction error distribution documented
- ✅ Anomaly detection ROC curve (AUC > 0.75)
- ✅ Test suite: synthetic data validation, model loading/inference
- ✅ Hyperparameter justification (no magic numbers)
- ✅ Phase 1.5 transfer learning path validated
- ✅ All imports pinned (requirements.txt with versions)

### Code Quality Standards
**Language:** Python 3.10+  
**Framework:** PyTorch (preferred) or TensorFlow 2.x  
**Style:** Black + isort, max line length 100  
**Type hints:** All function signatures  
**Testing:** pytest, >80% coverage on src/models/  
**Logging:** Structured logging (JSON), no print() statements  
**Documentation:** Docstrings for every class/function (Google style)

### Guardrails (Non-Negotiable)
1. **No pre-trained models without validation.**
   - Every model must include: reconstruction error curve + anomaly ROC curve + confusion matrix
   - Justification document required before merge

2. **Synthetic data assumptions must be explicit.**
   - Document: "We assume normal operation has mean_temp=24°C ± 2°C, humidity 70% ± 5%"
   - Challenge: "Where did these numbers come from? Valid for all crops?"
   - Review: Solution Architect validates before training on real customer data

3. **Model reproducibility is mandatory.**
   - Random seed fixed in code (np.random.seed + torch.manual_seed)
   - Any run with same seed → identical results
   - Notebook: "Train LSTM-VAE: 12 hours on GPU, final loss = 0.0234"

4. **Monthly model audit required.**
   - Check: reconstruction error trend, anomaly rate drift, false positive rate
   - If accuracy drops >5%, investigate + retrain
   - Document findings in monthly report

5. **Phase 1.5 transfer learning must be possible.**
   - Model must accept fine-tuning on new customer data
   - Checkpoint must load without errors into fine-tuning pipeline
   - Architecture decision: freezing which layers? Justify in ADR

### Anti-Patterns to Avoid
❌ Hard-coded thresholds for anomaly detection (should be data-driven)  
❌ Separate models per customer in Phase 1 (defeats transfer learning)  
❌ No version control on model checkpoints  
❌ Metrics only in notebooks (should be logged to database)  
❌ LSTM-VAE so tightly coupled with Isolation Forest that one can't be swapped  

---

## 2. Backend Engineer

### Profile
- **Title:** Backend/Data Engineer
- **Focus:** Data pipelines, API design, database, integration
- **Reports to:** Solution Architect (design oversight)
- **Dependencies:** ML Engineer (model inference), DevOps (infrastructure)

### Core Responsibilities
- [ ] Design canonical sensor schema (vendor-agnostic)
- [ ] Build data ingestion pipeline (CSV upload, MQTT listener, REST adapters)
- [ ] Set up PostgreSQL + TimescaleDB hypertables for time-series storage
- [ ] Create sensor data validation layer (schema enforcement, null handling)
- [ ] Design REST API for dashboard + inference queries
- [ ] Implement inference orchestration (call model, log results)
- [ ] Build feedback loop API (operator marks real/false)
- [ ] Create monitoring queries (ingestion health, inference latency)
- [ ] Document API contracts (OpenAPI/Swagger)
- [ ] Design for Priva/Hoogendoorn integration (Phase 1.5)

### Expected Output (Phase 1)
```
src/
├── api/
│   ├── main.py                    (FastAPI app)
│   ├── endpoints/
│   │   ├── sensor_data.py         (ingest CSV, MQTT)
│   │   ├── anomalies.py           (query alerts)
│   │   ├── feedback.py            (mark real/false)
│   │   └── health.py              (liveness probes)
│   └── schemas/
│       ├── sensor.py              (Pydantic models)
│       └── anomaly.py
├── database/
│   ├── models.py                  (SQLAlchemy ORM)
│   ├── migrations/
│   │   └── versions/
│   │       ├── 001_create_tables.py
│   │       └── 002_add_feedback_table.py
│   └── config.py                  (connection pooling)
├── ingestion/
│   ├── csv_parser.py              (validate, parse CSV)
│   ├── mqtt_listener.py           (subscribe topics)
│   ├── validation.py              (schema enforcement)
│   └── test_ingestion.py
├── inference/
│   ├── orchestrator.py            (call model, log results)
│   ├── correlation_engine.py      (rules evaluation)
│   └── test_orchestrator.py
└── utils/
    ├── logging_config.py
    ├── error_handlers.py
    └── retry_logic.py
```

### Definition of Done (BE Code)
- ✅ Unit tests for data validation, edge cases (>80% coverage)
- ✅ Integration tests (CSV upload → DB → query → API response)
- ✅ Database migration script tested (forward + rollback)
- ✅ Error handling comprehensive (missing sensors, timeouts, invalid formats)
- ✅ API documented (OpenAPI spec, examples for each endpoint)
- ✅ Performance verified (<200ms per request at 99th percentile)
- ✅ Connection pooling configured (no connection leaks)
- ✅ Logging structured (JSON format, traceable)
- ✅ All imports versioned (requirements.txt)

### Code Quality Standards
**Language:** Python 3.10+  
**Framework:** FastAPI (async, Pydantic validation)  
**Database:** SQLAlchemy ORM + Alembic migrations  
**Style:** Black + isort, max line length 100  
**Type hints:** Required for all functions  
**Testing:** pytest + pytest-asyncio  
**Async/await:** Consistently used throughout  
**Logging:** Python logging module, JSON formatter  
**API docs:** Auto-generated from Pydantic models

### Guardrails (Non-Negotiable)
1. **All API endpoints validate input.**
   - Pydantic models required for every request body
   - Return 422 for validation errors (not 500)
   - No raw SQL queries (ORM or parameterized only)

2. **Database migrations are versioned.**
   - No ad-hoc schema changes in production
   - All changes via Alembic migrations (checked into git)
   - Migration tested locally before deploy (forward + rollback)

3. **Every API call has timeout + retry logic.**
   - Timeout: 5 seconds for Priva API calls
   - Retry: 3 attempts with exponential backoff
   - Circuit breaker: stop retrying after 10 consecutive failures (60 sec cooldown)

4. **Sensor data is never silently discarded.**
   - If CSV has missing sensors: log warning, continue (don't fail)
   - If MQTT message malformed: log error, skip (don't crash broker)
   - Ingestion health monitored (alert if >1% of messages dropped)

5. **Schema evolution is backward-compatible.**
   - New sensor types added without breaking existing dashboards
   - Old API versions continue to work during transition
   - ADR required for any breaking change

### Anti-Patterns to Avoid
❌ Blocking API calls (always async)  
❌ Hard-coded database credentials (use environment variables)  
❌ No connection pooling (connection leaks)  
❌ Raw string interpolation in SQL (injection risk)  
❌ Silent failures (missing sensors logged as "OK")  
❌ Monolithic endpoint (sensor ingestion + inference + feedback in one route)  
❌ No circuit breaker for external APIs (cascading failures)  

---

## 3. DevOps/CS Lead

### Profile
- **Title:** DevOps/Customer Success Lead
- **Focus:** Infrastructure, reliability, customer operations
- **Reports to:** Solution Architect (operational oversight)
- **Dependencies:** Backend Engineer (deployment), ML Engineer (resource needs)

### Core Responsibilities
- [ ] Set up VPS infrastructure (Terraform)
- [ ] Configure PostgreSQL + TimescaleDB (replication, backups)
- [ ] Deploy Mosquitto MQTT broker (clustering, persistence)
- [ ] Set up monitoring (Prometheus + Grafana)
- [ ] Configure alerting (PagerDuty or similar)
- [ ] Implement backup/restore strategy (RTO: 1h, RPO: 15m)
- [ ] Create customer onboarding runbooks
- [ ] Manage customer credentials + API key rotation
- [ ] Monitor performance baselines (ingestion rate, inference latency)
- [ ] Respond to support tickets (triage, escalate)

### Expected Output (Phase 1)
```
infrastructure/
├── terraform/
│   ├── main.tf                    (VPS, security groups)
│   ├── database.tf                (PostgreSQL, backups)
│   ├── mqtt.tf                    (Mosquitto broker)
│   ├── monitoring.tf              (Prometheus, Grafana)
│   ├── variables.tf               (parameterized)
│   └── terraform.tfvars.example   (template)
├── docker/
│   ├── docker-compose.yml         (local dev environment)
│   ├── Dockerfile.api             (FastAPI container)
│   ├── Dockerfile.inference       (model inference worker)
│   └── mosquitto.conf             (broker config)
├── monitoring/
│   ├── prometheus.yml             (scrape configs)
│   ├── grafana_dashboards/
│   │   ├── ingestion_health.json
│   │   ├── api_latency.json
│   │   ├── model_performance.json
│   │   └── database_health.json
│   └── alerting_rules.yml         (alert conditions)
├── backup/
│   ├── backup.sh                  (daily backup script)
│   ├── restore.sh                 (restore procedure)
│   └── verify_backup.sh           (test backups weekly)
├── docs/
│   ├── runbook_customer_onboarding.md
│   ├── runbook_incident_response.md
│   ├── runbook_database_failover.md
│   └── runbook_model_update.md
└── scripts/
    ├── provision_customer.sh       (new customer setup)
    ├── rotate_credentials.sh       (monthly)
    └── health_check.sh             (daily verification)
```

### Definition of Done (Ops)
- ✅ Terraform plan reviewed + tested (no manual changes)
- ✅ Monitoring dashboard live (CPU, memory, disk, network, DB, MQTT)
- ✅ Alerting rules configured (P1: <5 min, P2: <30 min response)
- ✅ Backup script tested (restore succeeds on test environment)
- ✅ RTO/RPO verified (can restore within 1h/15m targets)
- ✅ Runbooks documented + peer-reviewed
- ✅ Customer credentials provisioned + documented
- ✅ Performance baselines established (ingestion rate, latency)

### Code Quality Standards
**Infrastructure as Code:** Terraform (no manual server changes)  
**Version control:** All IaC in git (pull request reviews required)  
**Documentation:** Runbooks in markdown, stored in wiki or repo  
**Naming:** Consistent naming convention (prefix_resource_env)  
**Security:** Secrets in environment variables, never in code  
**Testing:** Terraform plan reviewed before apply  

### Guardrails (Non-Negotiable)
1. **All infrastructure is version-controlled via Terraform.**
   - No SSH into servers to change configs
   - Every change → git commit → review → terraform apply
   - Can destroy and recreate environment from code

2. **Monitoring alerts configured BEFORE customer deployment.**
   - Alerting rules live in code (Prometheus + Grafana configs)
   - P1 incident: <4 hours response time (24/7 on-call during Phase 1)
   - Alert fatigue prevented: alerts fired only for actionable issues

3. **Daily backup verification required.**
   - Backup succeeds: logged + checked
   - Restore tested weekly on staging environment
   - RTO (Recovery Time Objective): 1 hour
   - RPO (Recovery Point Objective): 15 minutes

4. **Customer credentials are rotated monthly.**
   - API keys, database passwords rotated
   - Process: generate new → deploy → verify → rotate old
   - No downtime during rotation (blue-green strategy)

5. **Performance baselines tracked from day 1.**
   - Ingestion rate: target 1000 sensor reads/sec
   - API latency: p99 <200ms, p50 <50ms
   - Inference latency: <2 sec per anomaly detection
   - Database query latency: p99 <100ms for sensor lookups

### Anti-Patterns to Avoid
❌ Manual server changes (not reproducible)  
❌ Secrets in code or config files (security risk)  
❌ No monitoring until after customer complaint  
❌ Backups never tested (restore will fail when needed)  
❌ Runbooks not peer-reviewed (human error during crisis)  
❌ Single point of failure (no MQTT broker redundancy)  
❌ No circuit breaker logic (cascading outages)  

---

## 4. Solution Architect

### Profile
- **Title:** Solutions Architect / Technical Lead
- **Focus:** Architecture patterns, design oversight, decision documentation
- **Oversees:** All 3 engineers (code review, design review, decision making)
- **Reports to:** Product/CTO (project owner)

### Core Responsibilities
- [ ] Define canonical sensor schema (work with BE + ML)
- [ ] Document architectural patterns (data flow, error handling)
- [ ] Challenge design assumptions in team sync
- [ ] Review code for architectural debt (not just bugs)
- [ ] Write ADRs (Architecture Decision Records) for all major decisions
- [ ] Validate Phase 1.5 migration path (will Phase 1 code extend cleanly?)
- [ ] Identify anti-patterns early (tight coupling, premature optimization)
- [ ] Assess scalability impact of each design choice
- [ ] Conduct pre-implementation architecture review
- [ ] Maintain living diagram (data flow, deployment topology)

### Expected Output (Phase 1)
```
architecture/
├── adr/
│   ├── ADR-001_lstm_vae_vs_isolation_forest.md
│   ├── ADR-002_csv_plus_mqtt_dual_path.md
│   ├── ADR-003_postgresql_timescaledb_choice.md
│   ├── ADR-004_streamlit_vs_fastapi_for_dashboard.md
│   ├── ADR-005_canonical_schema_design.md
│   └── ADR-006_feedback_loop_architecture.md
├── diagrams/
│   ├── data_flow_architecture.svg
│   ├── api_contract.md              (OpenAPI spec)
│   ├── database_schema.sql
│   ├── error_handling_flow.svg
│   └── deployment_topology.svg
├── patterns/
│   ├── sensor_ingestion_pattern.md
│   ├── inference_orchestration_pattern.md
│   ├── feedback_loop_pattern.md
│   └── error_recovery_pattern.md
├── anti_patterns/
│   ├── anti_pattern_tight_coupling.md
│   ├── anti_pattern_monolithic_api.md
│   └── anti_pattern_silent_failures.md
└── phase_1_5_readiness/
    ├── migration_plan.md            (how Phase 1 extends to 1.5)
    ├── refactor_assessment.md       (estimated effort, blockers)
    └── scalability_analysis.md      (supports 10+ customers?)
```

### Definition of Done (Architecture)
- ✅ ADR filed for every major decision (context, decision, rationale, consequences)
- ✅ Data flow diagram shows path from sensor → model → alert
- ✅ API contract documented (request/response examples)
- ✅ Database schema reviewed (scalability, indexing)
- ✅ Error handling flows documented (what happens when X fails?)
- ✅ No architectural debt introduced (tight coupling flagged)
- ✅ Phase 1.5 path validated (migration estimated effort)
- ✅ Scalability concerns identified + mitigated

### Code Review Focus (Architectural)
When ML Engineer submits code:
- Does this follow canonical schema or create new silos?
- Can checkpoints be loaded in Phase 1.5 transfer learning?
- Are metrics logged in format that downstream systems can consume?
- Graceful degradation if training data corrupted?
- Reproducible run? Random seed + imports fixed?

When BE Engineer submits code:
- Does schema extend canonical model or break it?
- Tight coupling between ingestion + inference? (bad)
- Can Priva + Hoogendoorn both map into this schema? (Phase 1.5)
- Test coverage for error cases? (missing sensors, API timeouts)
- Latency budget met? (<200ms per request)

When DevOps submits infrastructure:
- Terraform code reproducible + reviewed?
- Monitoring alerts on right metrics (not noise)?
- Backup restore tested (not just successful)?
- Secrets not in code?
- Can restore to prior state if deployment breaks?

### Guardrails (Non-Negotiable)
1. **Every architecture decision gets an ADR.**
   - Template: context, decision, rationale, consequences, alternatives
   - Team must review + approve before code merge
   - ADR lives in repo (version controlled)

2. **Must conduct pre-implementation review of Phase 1.5 plan.**
   - By end of Phase 1 Week 6, you review Phase 1.5 architecture
   - Question: "Will this code refactor <20% or >50%?"
   - Identify blockers: tight coupling, schema mismatches
   - Plan remediation if needed

3. **Weekly architecture sync (30 min).**
   - Tuesday morning: ML, BE, DevOps, Architect
   - Agenda: new dependencies, schema changes, API contract changes, performance issues
   - Document decisions (ADR if major)

4. **Code review for architectural debt (not just syntax).**
   - Flag: monolithic functions (should be modular)
   - Flag: hard-coded values (should be parameterized)
   - Flag: duplicate logic (should be shared utility)
   - Flag: future scaling issues (will this work with 10+ customers?)

5. **Scalability assessment mandatory for every design choice.**
   - Question: "How does this scale from 1 customer to 10?"
   - If answer is "we'll need to refactor," document & plan mitigation now
   - Keep Phase 1.5 refactor effort < 20% of Phase 1 effort

### Anti-Patterns to Watch For
🚩 **Tight Coupling:** LSTM-VAE + Isolation Forest so linked that neither can be swapped  
🚩 **Monolithic API:** Single endpoint handles ingestion + feedback + query (hard to scale)  
🚩 **Silent Failures:** Missing sensors logged as warnings but model still runs (hides problems)  
🚩 **No Observability:** Metrics only in notebooks, not in database (can't track model drift)  
🚩 **Premature Optimization:** Vectorization before profiling (wastes effort)  
🚩 **Hard-Coded Thresholds:** Anomaly threshold = 0.5 (instead of data-driven)  
🚩 **No Versioning:** Model checkpoint without metadata (can't roll back)  

---

# Guardrails & Constraints

## Phase 1 Non-Negotiables

### Code Quality
- **Test coverage:** >80% on src/ directory (exclude notebooks)
- **Type hints:** All function signatures required
- **Imports:** Pinned versions (requirements.txt)
- **Linting:** Black + isort formatting, no lint errors
- **Documentation:** Every function has docstring (Google style)

### Architecture
- **No hard-coded values:** All config via environment variables or config files
- **Canonicalization:** Single sensor schema, extended (not forked) by each agent
- **Modularity:** One responsibility per module (LSTM-VAE separate from Isolation Forest)
- **Testability:** Code written so unit tests don't require external services (DB mocking)

### Operations
- **Infrastructure as Code:** 100% of infrastructure in Terraform (git version-controlled)
- **Monitoring from Day 1:** Prometheus + Grafana dashboards live before customer deploy
- **Backup Verification:** Weekly test restore (not just "backup succeeded")
- **Change Control:** Every infrastructure change via git PR + review

### Security
- **No secrets in code:** Environment variables only
- **API authentication:** Bearer token or OAuth (not hardcoded credentials)
- **Database permissions:** Least privilege (app user can't drop tables)
- **Encryption:** TLS for all external connections (MQTT, API, database)

### Communication
- **Written decisions:** ADRs in code repo (not Slack threads)
- **Code review:** Every PR reviewed by Solution Architect
- **Weekly sync:** Tuesday 30-min architecture sync (all 4 agents)
- **Escalation path:** Red flags → architecture review → team decision

---

# Coding Standards

## Python Code Style

### Structure
```python
# imports (ordered: stdlib, third-party, local)
import os
from typing import Optional, List
import pandas as pd
import torch
from src.config import settings

# constants
DEFAULT_TIMEOUT = 5
VALID_SENSOR_TYPES = {"temperature", "humidity", "co2"}

# classes
class SensorDataValidator:
    """Validates sensor data against canonical schema."""
    
    def __init__(self, schema: dict):
        self.schema = schema
    
    def validate(self, data: dict) -> bool:
        """Check if data matches schema."""
        pass

# functions
def parse_csv(filepath: str) -> pd.DataFrame:
    """Load CSV and return validated dataframe."""
    pass

# main
if __name__ == "__main__":
    pass
```

### Type Hints
```python
def calculate_reconstruction_error(
    original: torch.Tensor,
    reconstructed: torch.Tensor,
    reduction: str = "mean"
) -> float:
    """Calculate MSE between original and reconstructed tensors.
    
    Args:
        original: Input tensor of shape (batch_size, seq_len, features)
        reconstructed: Output tensor, same shape
        reduction: "mean" or "sum"
    
    Returns:
        Scalar error value (float)
    
    Raises:
        ValueError: If tensors don't match shape
    """
    if original.shape != reconstructed.shape:
        raise ValueError(f"Shape mismatch: {original.shape} vs {reconstructed.shape}")
    
    mse = torch.mean((original - reconstructed) ** 2)
    return mse.item()
```

### Testing
```python
# tests/test_models.py
import pytest
from src.models import LSTMVae

@pytest.fixture
def lstm_vae():
    return LSTMVae(input_size=10, latent_size=5)

def test_forward_pass(lstm_vae):
    """Test that model can do forward pass."""
    import torch
    x = torch.randn(16, 24, 10)  # (batch, seq_len, features)
    recon, latent = lstm_vae(x)
    assert recon.shape == x.shape
    assert latent.shape == (16, 5)

def test_reproducibility():
    """Test that same seed gives same results."""
    torch.manual_seed(42)
    m1 = LSTMVae(10, 5)
    out1 = m1(torch.randn(1, 24, 10))
    
    torch.manual_seed(42)
    m2 = LSTMVae(10, 5)
    out2 = m2(torch.randn(1, 24, 10))
    
    assert torch.allclose(out1[0], out2[0])
```

### Logging
```python
import logging
import json

logger = logging.getLogger(__name__)

# structured logging
logger.info(json.dumps({
    "event": "model_trained",
    "epoch": 10,
    "loss": 0.0234,
    "timestamp": "2026-05-13T10:30:00Z"
}))

# don't do this
print("Model trained!")  # ❌ hard to parse, no timestamp
```

---

# Decision Workflows

## Workflow 1: Evaluating ML Model Architecture

**Scenario:** Should we use LSTM-VAE or XGBoost for anomaly detection?

**Step 1: ML Engineer proposes**
- "I recommend LSTM-VAE because it learns latent representations of normal patterns, enabling early anomaly detection even when individual sensors look normal (e.g., NOx contamination)."

**Step 2: Solution Architect questions**
- "Why LSTM-VAE over Isolation Forest (faster, less data-hungry)?"
- "What's the latency budget for inference? 2 seconds is too slow for real-time alerts."
- "How does this generalize from synthetic data to real customer data?"

**Step 3: Team discusses**
- ML: "LSTM can't be beat for temporal patterns, but Isolation Forest is faster. Maybe ensemble?"
- BE: "Ensemble means two model calls. Can we handle that latency?"
- DevOps: "Model inference runs on customer's Jetson Nano. What's the power draw?"

**Step 4: Decision documented (ADR)**
```markdown
# ADR-001: LSTM-VAE + Isolation Forest Ensemble

## Context
Need anomaly detection that catches infrastructure failures invisible to traditional sensors.
E.g., NOx contamination detected by visual pattern (plant stress) + physical ratio violations.

## Decision
Use LSTM-VAE as primary (learns temporal patterns) + Isolation Forest as secondary (fast rule-based).
Ensemble: if either flags anomaly AND other agrees, alert.

## Rationale
- LSTM-VAE: learns what "normal" looks like, catches invisible failures
- Isolation Forest: fast (no GPU needed), catches physical ratio violations
- Ensemble: reduces false positives, improves confidence
- Trade-off: slightly higher latency (2 model calls), but under 2 sec budget

## Consequences
- Fine-tuning both models in Phase 1.5
- Inference latency: ~1.8 sec on Jetson Nano
- False positive rate: target <5% (need operator feedback to refine)

## Alternatives Considered
- XGBoost only: faster, but doesn't learn temporal patterns
- LSTM-VAE only: good but no safety net for physical ratios
- One-class SVM: less interpretable, harder to debug
```

---

## Workflow 2: Code Review for Architectural Issues

**Scenario:** ML Engineer submits LSTM-VAE training code for review.

**Architect's Checklist:**
```
Architecture Review: LSTM-VAE Model Code
├─ Does this follow canonical schema?
│  └─ Input: (batch_size, seq_len, num_sensors)
│  └─ Canonical sensors: temp, humidity, co2, nox, power, water
│  └─ ✓ Good, schema extended correctly
│
├─ Can this be fine-tuned in Phase 1.5?
│  └─ Checkpoint saved with: model state, training metadata
│  └─ Transfer learning: freeze encoder, fine-tune decoder?
│  └─ ⚠️ Question: which layers frozen? Document in ADR
│
├─ Are metrics logged for downstream systems?
│  └─ Reconstruction error logged to database (not just notebook)
│  └─ Anomaly scores stored (so Phase 2 LLM can learn from outcomes)
│  └─ ✓ Good, metrics available for Phase 2
│
├─ Graceful degradation if data corrupted?
│  └─ What if CSV has NaN values?
│  └─ What if training data has outliers?
│  └─ ✓ Good, validation in ingestion pipeline
│
├─ Reproducibility?
│  └─ Random seed fixed (np.random.seed + torch.manual_seed)
│  └─ Requirements.txt pinned
│  └─ ✓ Excellent, can reproduce 3 months later
│
├─ Scalability concerns?
│  └─ How does model perform with 10 greenhouses?
│  └─ Will one model per greenhouse or one shared model?
│  └─ ⚠️ Question: Phase 1.5 multi-customer strategy?
│
└─ Alternatives considered?
   └─ ADR discusses why LSTM-VAE over Isolation Forest only
   └─ ✓ Documented, team aware
```

**Feedback to ML Engineer:**
- ✅ Approve with comments
- Add: "In ADR-001, clarify which layers frozen for Phase 1.5 transfer learning"
- Add: "Document the 3 synthetic data assumptions (temp range, humidity range, etc.)"

---

# Architecture Decision Records (ADR)

## ADR Template

```markdown
# ADR-NNN: [Title: one sentence decision]

## Status
[ Proposed | Accepted | Superseded by ADR-XXX ]

## Context
[ Why are we deciding now? What triggered this decision? ]

Example:
  We need anomaly detection that catches infrastructure failures
  not visible to traditional SCADA sensors (Priva, Hoogendoorn).
  E.g., NOx contamination detected by plant visual stress + ratio violations.

## Decision
[ What did we choose? Be specific. ]

Example:
  Use LSTM-VAE as primary anomaly detector, Isolation Forest as secondary.
  Ensemble: alert if both flag anomaly.

## Rationale
[ Why this choice? What trade-offs did we accept? ]

Example:
  - LSTM-VAE learns temporal patterns and latent space of "normal" operation
  - Can detect invisible failures (NOx, boiler degradation) via reconstruction error
  - Isolation Forest acts as safety net: catches physical ratio violations fast
  - Ensemble reduces false positives
  - Trade-off: inference latency ~1.8 sec (acceptable for greenhouse monitoring)

## Consequences
[ What becomes easier/harder? What's the cost? ]

Example:
  - Easier: catch infrastructure failures 24-48h early
  - Easier: interpretable alerts (show which ratios violated + reconstruction error)
  - Harder: need to fine-tune two models for each customer (Phase 1.5 effort +20%)
  - Harder: model maintenance complexity (two separate systems)
  - Cost: inference latency, GPU/Jetson Nano power draw

## Alternatives Considered
[ What didn't we choose and why? ]

Example:
  1. XGBoost only: faster (~500ms), but no temporal pattern learning
  2. LSTM-VAE only: good but no rule-based safety net
  3. One-class SVM: less interpretable, harder to debug
  4. Hand-crafted rules only: fast but doesn't learn from data

## Related ADRs
[ Links to decisions that depend on this one ]

Example:
  - ADR-002: CSV + MQTT dual-path data ingestion
  - ADR-005: Canonical sensor schema (feeds this model)
```

## Sample ADRs to Write (Phase 1)

### ADR-001: LSTM-VAE + Isolation Forest Ensemble
**Owner:** ML Engineer  
**Due:** Week 2  
**Blocks:** Model training implementation  

### ADR-002: CSV + MQTT Dual-Path Data Ingestion
**Owner:** Backend Engineer  
**Due:** Week 1  
**Blocks:** Data pipeline implementation  

### ADR-003: PostgreSQL + TimescaleDB for Time-Series Storage
**Owner:** Backend Engineer + DevOps  
**Due:** Week 1  
**Blocks:** Database schema design  

### ADR-004: Streamlit Dashboard (MVP) vs. React (Phase 2)
**Owner:** Solution Architect  
**Due:** Week 1  
**Blocks:** Dashboard implementation  

### ADR-005: Canonical Sensor Schema (Vendor-Agnostic)
**Owner:** Solution Architect + Backend Engineer  
**Due:** Week 0 (pre-implementation)  
**Blocks:** Everything else  

### ADR-006: Feedback Loop Architecture (Operator Marks Real/False)
**Owner:** Backend Engineer  
**Due:** Week 4  
**Blocks:** Feedback ingestion + model retraining  

---

# Weekly Sync & Review Checklist

## Weekly Architecture Sync (Tuesday, 30 min)

**Attendees:** ML Engineer, Backend Engineer, DevOps, Solution Architect  
**Goal:** Identify architectural issues early, make decisions, document

### Agenda Template
```
1. Standup (5 min)
   ├─ ML: LSTM-VAE training status, any blocker?
   ├─ BE: API endpoints done, any schema questions?
   ├─ DevOps: Infrastructure ready, monitoring running?
   └─ Architect: Code review findings, ADRs needed?

2. Architectural Review (15 min)
   ├─ Any new dependencies added? Justify.
   ├─ Database schema changes? Review migration.
   ├─ API contract changes? Breaking changes?
   ├─ New error modes identified? SLA impact?
   ├─ Model accuracy trend? Regression detected?
   ├─ Performance baseline hit? Optimization needed?
   └─ Phase 1.5 risk assessment (ongoing)

3. Decisions (8 min)
   ├─ Document any decisions as ADR
   ├─ Assign action items
   └─ Update project tracker

4. Next week priorities (2 min)
```

### Review Checklist (Every PR)

**Before Code Review:**
- [ ] PR title clearly describes what changed
- [ ] ADR filed (if architectural decision)
- [ ] Tests added (not just existing tests pass)

**During Code Review (Architect):**
- [ ] Canonical schema followed (or documented extension)
- [ ] No hard-coded values (config via environment)
- [ ] Modularity: one responsibility per function/class
- [ ] Test coverage >80% on changed files
- [ ] Type hints on all function signatures
- [ ] Error handling for edge cases documented
- [ ] Phase 1.5 compatibility considered

**Before Merge:**
- [ ] All conversations resolved
- [ ] At least 2 approvals (code quality + architecture)
- [ ] CI/CD pipeline passes (tests, lint, type check)
- [ ] ADR approved (if applicable)

---

# Escalation & Red Flags

## Red Flags (Immediate Halt, Team Meeting Required)

### 🛑 Architectural Blocker: Tight Coupling
**Symptom:** LSTM-VAE and Isolation Forest so intertwined that changing one breaks the other  
**Why it matters:** Phase 1.5 needs to ensemble both models independently  
**Action:** Stop work, architect designs decoupling, team estimates refactor  

### 🛑 Architectural Blocker: Database Schema Can't Scale
**Symptom:** Schema assumes 1 customer, hard-coded customer_id  
**Why it matters:** Phase 1.5 needs to support 5+ customers simultaneously  
**Action:** Architect proposes schema redesign, validate with BE + DevOps  

### 🛑 Operational Risk: No Monitoring
**Symptom:** Inference latency unknown, ingestion rate unmeasured  
**Why it matters:** Can't debug performance issues after deploy  
**Action:** DevOps + Architect define SLIs before customer deploy  

### 🛑 Model Risk: No Versioning
**Symptom:** Model checkpoint saved, but no metadata (training data, hyperparams, metrics)  
**Why it matters:** Can't debug if accuracy degrades, can't roll back  
**Action:** ML Engineer documents checkpoint format, schema for metadata  

## Warnings (Discuss Before Merge)

### ⚠️ Performance: Premature Optimization
**Symptom:** Code vectorized before profiling shows it's a bottleneck  
**Why it matters:** Wastes effort, makes code harder to read  
**Action:** Architect asks: "Where's the profiling data?" If none, revert to clear code  

### ⚠️ Architecture: Monolithic API
**Symptom:** Single `/ingest` endpoint handles validation + model call + feedback  
**Why it matters:** Hard to test, hard to scale, mixing concerns  
**Action:** Architect proposes: separate endpoints per concern  

### ⚠️ Data: Silent Failures
**Symptom:** Missing sensor in CSV logged as warning, model still runs  
**Why it matters:** Model trains on partial data, accuracy suffers silently  
**Action:** Architect proposes: fail loudly (exception), let operator fix CSV  

### ⚠️ Security: Secrets in Config Files
**Symptom:** Database password in docker-compose.yml  
**Why it matters:** Secrets leak if config file committed  
**Action:** DevOps enforces environment variables + `.gitignore`  

### ⚠️ Maintainability: Hard-Coded Thresholds
**Symptom:** Anomaly threshold = 0.5 (hard-coded in model)  
**Why it matters:** Can't tune per-customer in Phase 1.5  
**Action:** Architect proposes: parameterize as config value  

---

# Phase 1.5 Readiness Assessment (Due Week 6 of Phase 1)

**Architect conducts final assessment:**

## Checklist
- [ ] Can Phase 1 model be fine-tuned without major refactor?
- [ ] Are all metrics logged in database (not just notebooks)?
- [ ] Is canonical schema extensible to Priva + Hoogendoorn?
- [ ] API contract backward-compatible for Phase 2 LLM agent?
- [ ] Infrastructure reproducible from code (no manual steps)?
- [ ] Support runbooks tested + peer-reviewed?
- [ ] Customer feedback loop working (marks collected + analyzed)?

## Readiness Verdict
- 🟢 **Ready:** <10% refactor effort, proceed to Phase 1.5
- 🟡 **Hold:** 10-20% refactor estimated, address blockers, reassess
- 🔴 **Not Ready:** >20% refactor, needs architecture rework before Phase 1.5

---

# Appendix: Tools & Environments

## Development Environment Setup
```bash
# Python version: 3.10+
python --version

# Virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run tests
pytest tests/ -v --cov=src/ --cov-report=term-missing

# Lint
black src/ tests/
isort src/ tests/
pylint src/

# Type check
mypy src/
```

## Database Setup
```bash
# PostgreSQL + TimescaleDB (via Docker)
docker run -d \
  --name postgres-tsdb \
  -e POSTGRES_PASSWORD=dev_password \
  -p 5432:5432 \
  timescale/timescaledb:latest-pg15

# Connect
psql -h localhost -U postgres -d postgres

# Run migrations
alembic upgrade head
```

## Monitoring Setup
```bash
# Prometheus (scrape metrics)
docker run -d \
  --name prometheus \
  -v $(pwd)/prometheus.yml:/etc/prometheus/prometheus.yml \
  -p 9090:9090 \
  prom/prometheus

# Grafana (visualize)
docker run -d \
  --name grafana \
  -p 3000:3000 \
  grafana/grafana
```

---

**Document Status:** Ready for team onboarding  
**Last updated:** May 13, 2026  
**Next review:** After Phase 1 Week 2 (architecture checkpoint)

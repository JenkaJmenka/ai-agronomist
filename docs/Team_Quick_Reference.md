# Agent Team — Quick Reference & Communication Protocol

**Use this guide for day-to-day decisions and escalations**  
**Last updated:** May 13, 2026

---

## Quick Decision Matrix: Who Decides What?

### Code Decisions

| Decision Type | Owner | Consulted | Final Approval |
|---------------|-------|-----------|-----------------|
| Model architecture (LSTM-VAE vs. XGBoost) | ML Engineer | Architect, BE | Architect (ADR) |
| Model hyperparameters (learning rate, batch size) | ML Engineer | — | ML Engineer (test results) |
| Database schema design | BE Engineer | ML Engineer, DevOps | Architect (scalability review) |
| API endpoint design | BE Engineer | Architect, ML | Architect (consistency review) |
| Infrastructure code (Terraform) | DevOps | Architect, BE | Architect (reproducibility review) |
| Error handling strategy | BE Engineer | Architect, DevOps | Architect (consistency review) |
| Testing strategy (unit vs. integration) | All | — | Architect (coverage targets) |
| Code style & formatting | All | — | CI/CD checks (Black, isort) |

### Architectural Decisions

| Decision Type | Owner | Consulted | Final Approval |
|---------------|-------|-----------|-----------------|
| Major design choice (streaming vs. batch) | Architect | All 3 engineers | Team consensus (in sync) |
| Phase 1.5 readiness assessment | Architect | All 3 engineers | Architect (formal verdict) |
| Trade-off decision (speed vs. accuracy) | Architect | All 3 engineers | Team consensus (documented in ADR) |
| Scalability concern resolution | Architect | ML Engineer, BE | Architect (validates solution) |
| Anti-pattern remediation | Architect | Relevant engineer | Architect (approves refactor) |

### Operational Decisions

| Decision Type | Owner | Consulted | Final Approval |
|---------------|-------|-----------|-----------------|
| Customer onboarding process | DevOps | Architect, CS team | DevOps (runbook finalized) |
| Incident response | DevOps | Architect (severity P1) | DevOps (with escalation to Architect for P1) |
| Backup/restore procedure | DevOps | Architect (validation plan) | DevOps (weekly test completion) |
| Monitoring alerts setup | DevOps | Architect (criticality) | Architect (approves alert rules) |
| Performance baseline targets | DevOps + Architect | BE Engineer (for latency) | Architect (SLA finalized) |

---

## When to Escalate (Escalation Path)

### Tier 1: Engineer Decides (No Escalation Needed)
- Single sensor data validation rule (e.g., temperature must be 0-50°C)
- Database index optimization (not schema change)
- Monitoring dashboard layout (visualization preference)
- Utility function refactoring (no architectural impact)

**Action:** Commit code, PR review by peer, merge if approved

---

### Tier 2: Architect Reviews (Must Get Approval)
- New API endpoint design
- Database schema changes (columns, tables)
- Model architecture choices
- Infrastructure code (Terraform)
- Any code that will be difficult to refactor in Phase 1.5

**Action:** File ADR (if major), get architect approval before merge

---

### Tier 3: Team Decision (Sync Meeting Required)
- Major trade-off (latency vs. accuracy, simplicity vs. generalization)
- Multi-engineer impact (e.g., BE change affects ML training pipeline)
- Phase 1.5 readiness assessment
- Scalability concerns (will this work for 10+ customers?)
- Resource contention (one engineer blocked by another)

**Action:** Add to Tuesday sync agenda, discuss 15 min, document decision as ADR

---

### Tier 4: Escalation to Product/CTO (Team Can't Resolve)
- Scope creep (feature requests during Phase 1)
- Timeline slippage (can't hit Week 8 milestone)
- Customer conflict (customer wants feature not in scope)
- Resource constraint (need to hire, can't proceed)

**Action:** Escalate with impact assessment, recommend solution, let product decide

---

## Communication Protocol

### Synchronous Communication (Immediate)

**Use for:** Urgent blockers, real-time problem-solving, design discussions  
**Channel:** Slack (Slack thread, not DM) or video call  
**Expected response:** <1 hour  
**Examples:**
- "API endpoint returning 500s in prod, investigating"
- "LSTM-VAE training diverged, trying new learning rate"
- "Database migration failed in staging, help needed"

**Slack thread protocol:**
```
@team-ml
[Issue]: Model inference latency 5s, target <2s
[Context]: Batch size 32, inference on Jetson Nano
[What I've tried]: Reduced batch to 16 (no change), checked GPU memory (OK)
[Blocker]: Need DevOps to check CPU/memory profiling
[Action needed]: Can you run nvidia-smi on Jetson?
[Timeline]: Urgent (customer demo tomorrow)
```

---

### Semi-Synchronous (Decision Within Hours)

**Use for:** Design decisions, architecture questions, code review feedback  
**Channel:** GitHub PR review, Slack with thread  
**Expected response:** <4 hours  
**Examples:**
- Code review asking for refactoring
- ADR review with questions
- API contract change proposal

**GitHub PR review protocol:**
```
## Architecture Review

**Question 1:** Why are LSTM-VAE and Isolation Forest tightly coupled?
- This prevents Phase 1.5 from using either independently
- Suggest: Extract to separate classes, call independently

**Question 2:** Is reconstruction error logged to database?
- Need this for Phase 2 LLM to learn from outcomes
- Suggest: Add logging in orchestrator.py

**Question 3:** Error handling for corrupted CSV?
- Currently fails silently if missing columns
- Suggest: Validate schema, raise ValueError with clear message

Approval: Will approve once above addressed
```

---

### Asynchronous (Decision Within 1-2 Days)

**Use for:** Documentation updates, non-urgent questions, FYI updates  
**Channel:** Slack comment, ADR comment, weekly sync notes  
**Expected response:** 1-2 days  
**Examples:**
- Weekly progress update
- ADR filed for feedback
- Documentation update

**ADR review protocol (async):**
```
## ADR-001 Review Comments

@ml-engineer: Questions on your LSTM-VAE choice:

1. Why not Isolation Forest only?
   - Response: Too many false positives on ratio violations, see test results

2. Won't two models double inference latency?
   - Response: Yes ~1.8s, acceptable for 15-min monitoring interval

3. How does fine-tuning work in Phase 1.5?
   - Response: Plan to freeze encoder, fine-tune decoder on customer data

Please update ADR with clarifications, then I'll approve.
```

---

## Definition of "Done" by Role

### ML Engineer

**Code is "Done" when:**
- [ ] Model checkpoint saved with metadata (training data, hyperparams, metrics)
- [ ] Reconstruction error plot + anomaly ROC curve included
- [ ] Unit tests pass (>80% coverage on src/models/)
- [ ] Reproducible: same random seed → same results
- [ ] All imports pinned in requirements.txt
- [ ] Docstrings on all functions (Google style)
- [ ] ADR filed explaining architecture choices
- [ ] Architect has approved (code review + ADR review)

**Runbook: "Model Training Complete"**
```
1. Train LSTM-VAE on synthetic data: 12 hours
2. Evaluate on held-out test set: AUC > 0.75 (yes/no)
3. Save checkpoint: models/lstm_vae_v1.0.pth
4. Save metadata: models/model_metadata.json
5. Generate plots: reconstruction error, ROC curve, confusion matrix
6. Unit tests pass: pytest src/models/ --cov >80%
7. Announce in #ml-updates: "LSTM-VAE ready for integration testing"
8. Code review: ML engineer awaits Architect approval
```

---

### Backend Engineer

**Code is "Done" when:**
- [ ] API endpoints accept & validate input (Pydantic models)
- [ ] Database schema migrations tested (forward + rollback)
- [ ] Error handling for all edge cases (missing sensors, timeouts, invalid formats)
- [ ] Tests pass (>80% coverage, including integration tests)
- [ ] API documented (OpenAPI/Swagger with examples)
- [ ] Performance verified (<200ms p99 latency)
- [ ] Connection pooling + timeout logic configured
- [ ] All imports pinned in requirements.txt
- [ ] Architect has approved (code review + design review)

**Runbook: "API Integration Complete"**
```
1. Implement sensor ingestion endpoint: POST /api/v1/sensors
2. Add Pydantic validation: validate schema, required fields, ranges
3. Implement database insert: sensors table + error logging
4. Add error handling: null values, type mismatches, timeouts
5. Write tests: valid input, invalid input, edge cases, performance
6. Generate OpenAPI spec: test with Swagger UI
7. Performance test: 1000 requests/sec, p99 latency <200ms
8. Unit tests pass: pytest tests/ --cov >80%
9. Announce in #api-updates: "Sensor ingestion ready for E2E test"
10. Code review: BE engineer awaits Architect approval
```

---

### DevOps/CS Lead

**Infrastructure is "Done" when:**
- [ ] Terraform code reviewed & tested (terraform plan, no errors)
- [ ] Monitoring dashboards live (CPU, memory, disk, network, DB, MQTT)
- [ ] Alerting rules configured (P1: <5 min response time targets)
- [ ] Backup script tested (restore succeeds on test environment)
- [ ] RTO/RPO verified (restore within 1h/15m)
- [ ] Customer onboarding runbook documented & peer-reviewed
- [ ] Performance baselines established (ingestion rate, latency)
- [ ] All secrets in environment variables (not in code)
- [ ] Architect has approved (infrastructure review)

**Runbook: "Infrastructure Ready for Customer Deploy"**
```
1. Provision VPS: Terraform apply (save state file, commit to git)
2. Setup PostgreSQL + backups: manual restore test successful
3. Deploy Mosquitto broker: test MQTT topic subscription
4. Configure Prometheus: scraping metrics from all services
5. Build Grafana dashboards: ingestion rate, API latency, model performance
6. Setup alerting: PagerDuty integration, test alert firing
7. Document customer onboarding: step-by-step runbook
8. Test customer setup: provision test customer, verify end-to-end
9. Terraform destroy & recreate: verify reproducibility
10. Announce in #ops-updates: "Infrastructure ready for customer deploy"
11. Architect approves: operational readiness sign-off
```

---

### Solution Architect

**Architecture is "Done" when:**
- [ ] ADR filed for every major decision (context, decision, rationale, consequences)
- [ ] Data flow diagram shows path from sensor → model → alert
- [ ] API contract documented (request/response examples, error codes)
- [ ] Database schema reviewed for scalability (can handle 10+ customers?)
- [ ] Code review completed (no tight coupling, no architectural debt)
- [ ] Phase 1.5 migration path validated (estimated refactor <20%)
- [ ] Scalability concerns identified & mitigated
- [ ] Weekly sync notes documented (decisions + action items)

**Runbook: "Architecture Review Complete"**
```
1. Read all ADRs filed this week: any red flags?
2. Review code PRs: check for architectural debt, tight coupling
3. Database schema: can it scale to 10 customers? Indexes optimized?
4. API contract: backward-compatible for Phase 2? Breaking changes?
5. Error handling: all failure modes documented? SLA impact clear?
6. Performance: latency budget met? Monitoring in place?
7. Phase 1.5 readiness: list refactoring needed, estimate effort
8. Weekly sync: document decisions, update project tracker
9. Announce in #architecture-updates: "Weekly architecture review complete"
```

---

## Red Flags Checklist (What to Escalate Immediately)

### 🔴 P1 Red Flags (Halt & Escalate)
- [ ] Tight coupling between LSTM-VAE + Isolation Forest (prevents swapping)
- [ ] Database schema assumes single customer (won't scale to 5+)
- [ ] No monitoring → inference latency unknown
- [ ] Model checkpoint has no metadata → can't roll back
- [ ] Secrets in code or config files
- [ ] <50% test coverage on critical paths
- [ ] No error handling for external API failures (Priva timeout)

**Action:** Slack thread with @architect, schedule sync within 2 hours

---

### 🟡 P2 Red Flags (Discuss Before Merge)
- [ ] Premature optimization without profiling data
- [ ] Monolithic API endpoint (mixing concerns)
- [ ] Silent failures (missing sensors logged as warnings, model still runs)
- [ ] Hard-coded thresholds instead of configurable
- [ ] No test coverage for new code
- [ ] Code duplicated in multiple places
- [ ] Database query without index (will be slow at scale)

**Action:** GitHub PR comment, request changes before merge

---

## Suggested Sprint Structure (Phase 1)

### Week 1: Foundation & Architecture
- **ML:** Synthetic data generation, LSTM-VAE skeleton
- **BE:** Canonical schema design, API skeleton
- **DevOps:** Infrastructure Terraform, monitoring setup
- **Architect:** File ADRs (001-005), validate canonical schema
- **Sync:** Tuesday — validate schema is shared, catch early misalignment

### Week 2-3: Implementation
- **ML:** LSTM-VAE training, Isolation Forest training
- **BE:** CSV ingestion, API endpoints, database migrations
- **DevOps:** Customer provisioning scripts, backup automation
- **Architect:** Code reviews, assess Phase 1.5 refactor risk
- **Sync:** Tuesday — discuss latency budget, error handling strategy

### Week 4-5: Integration Testing
- **ML:** Model inference testing, metric logging
- **BE:** End-to-end API test (CSV → DB → inference → alert)
- **DevOps:** E2E monitoring test, alert firing verification
- **Architect:** Final code review, Phase 1.5 readiness assessment begins
- **Sync:** Tuesday — performance baselines met? Any blockers?

### Week 6-7: Beta Customer Deploy
- **ML:** Monitor model performance on real data
- **BE:** API stability, error handling under load
- **DevOps:** Customer support, incident response
- **Architect:** Assess Phase 1.5 refactor needed, finalize readiness
- **Sync:** Tuesday — customer feedback, phase 1.5 prep

### Week 8: Phase 1 Retrospective
- **All:** Document learnings, update architecture patterns
- **Architect:** Phase 1.5 readiness verdict (green/yellow/red)
- **Sync:** Tuesday — retrospective, Phase 1.5 kickoff planning

---

## Key Metrics to Track

### ML Engineer
- Model accuracy (reconstruction error, AUC, false positive rate)
- Training time (hours to train on synthetic data)
- Inference latency (milliseconds per prediction)
- Model size (MB checkpoint)

### Backend Engineer
- API latency (p50, p99 response time)
- Ingestion rate (sensor readings/sec)
- Test coverage (%)
- Error rate (% of requests that error)

### DevOps
- Infrastructure uptime (%)
- Backup success rate (%)
- Restore time (minutes to recover)
- Monitoring alert accuracy (% false positives)

### Solution Architect
- ADR compliance (% of decisions documented)
- Code review turnaround (<24 hours)
- Architectural debt identified (# of red flags caught)
- Phase 1.5 refactor estimate (% of Phase 1 effort)

---

## Emergency Contact Protocol

### P1 Incident (Production Down, Customer Impacted)
**Response time: <1 hour**

1. DevOps identifies incident (monitoring alert fired)
2. Slack: #incidents channel, tag @on-call
3. Conference call: DevOps + Architect + relevant engineer (ML or BE)
4. Investigation: <30 min determine root cause
5. Fix: implement + verify
6. Postmortem: document + assign follow-up

Example:
```
[P1 INCIDENT] API returning 500s
Time: 2026-05-13 14:30 UTC
Duration: 15 minutes
Impact: Customer dashboard unavailable
Root cause: PostgreSQL connection pool exhausted
Fix: Restarted API service, increased pool size
Postmortem: Link to incident doc, follow-up: improve monitoring
```

---

### P2 Bug (Feature Degradation, No Customer Impact)
**Response time: <4 hours**

1. Engineer creates GitHub issue
2. Architect reviews for severity
3. Assign to sprint or backlog
4. Fix in next sprint

---

### P3 Question (No Impact)
**Response time: <24 hours**

1. Ask in Slack thread
2. Expected: async response, no meeting needed

---

## How to Read This Guide

**If you're a...**

**ML Engineer:** Jump to "Code Decisions" matrix, your "Definition of Done" section, and the "Red Flags Checklist" (P1 items).

**Backend Engineer:** Jump to "Code Decisions" matrix, your "Definition of Done" section, and the "Red Flags Checklist" (P1 items).

**DevOps Lead:** Jump to "Operational Decisions" matrix, your "Definition of Done" section, and the "Red Flags Checklist" (especially monitoring, backup, secrets).

**Solution Architect:** Start at "Escalation Path" (you approve all Tier 2+3 decisions), then review "Red Flags Checklist" (you catch all of these), and finally "Weekly Architecture Sync" (your role leading it).

---

## Last Updated
May 13, 2026 by Solution Architect  
Next review: End of Phase 1 Week 2

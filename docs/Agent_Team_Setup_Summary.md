# AI Agronomist Phase 1 — Agent Team Setup Summary

**Status:** Team configuration complete, ready for Phase 1 kickoff  
**Date:** May 13, 2026  
**Next step:** Week 0 sprint planning

---

## What You Have

### 1. Interactive Team Overview (Artifact)
Visual dashboard showing:
- 4 agent personas (ML Engineer, BE Engineer, DevOps, Architect)
- Skills & responsibilities per role
- Guardrails (non-negotiables)
- Definition of "done" for each role
- Solution Architect role explained
- Weekly review checklist
- Decision workflows
- Code review checklist
- Red flags that trigger escalation

**Location:** Embedded widget in chat (3 tabs: Team Overview, Architect Role, Decision Workflows)

---

### 2. Agent Team Reference Guide (Full Documentation)
Comprehensive guide covering:
- **Agent Personas:** Profile, responsibilities, expected outputs, DoD, code standards, guardrails, anti-patterns
- **Guardrails:** Phase 1 non-negotiables (code quality, architecture, operations, security, communication)
- **Coding Standards:** Python style, type hints, testing, logging
- **Decision Workflows:** 2 worked examples (ML model choice, code review process)
- **ADR Template:** How to document architectural decisions
- **Weekly Sync Checklist:** What to review, when, how
- **Escalation & Red Flags:** What stops work, what's just a warning

**Location:** Agent_Team_Reference_Guide.md (full 500+ line reference document)

---

### 3. Team Quick Reference (Day-to-Day Operations)
Quick lookup guide for decisions, communication, and execution:
- **Decision Matrix:** Who decides what (code, architecture, operations)
- **Escalation Path:** Tier 1-4 escalation criteria and actions
- **Communication Protocol:** Sync vs. async, channels, response times, message format
- **Definition of Done:** Runbook for each role (checklist format)
- **Red Flags Checklist:** P1 (halt immediately) vs. P2 (discuss before merge)
- **Sprint Structure:** Suggested week-by-week plan for Phase 1
- **Key Metrics:** What each role should track
- **Emergency Contact Protocol:** P1/P2/P3 incident response

**Location:** Team_Quick_Reference.md (quick lookup, bookmark this!)

---

## How to Use These Documents

### Week 0: Pre-Implementation
1. **All team members:** Read "Agent Team Reference Guide" (focus on your role section)
2. **All team members:** Bookmark "Team Quick Reference" (you'll use it daily)
3. **Solution Architect:** Prepare 5 ADRs to file in Week 1 (ADR-001 through ADR-005)
4. **Team sync:** Tuesday meeting to confirm shared understanding

### Week 1-8: Implementation
1. **Every engineer:** Consult "Definition of Done" (your role) before marking code complete
2. **Every PR:** Solution Architect uses "Code Review Focus" checklist
3. **Weekly Tuesday sync:** Use "Weekly Sync Checklist" as agenda template
4. **Any blockers:** Consult "Escalation Path" (which tier?), then escalate

### End of Phase 1
1. **Solution Architect:** Conduct "Phase 1.5 Readiness Assessment"
2. **All team:** Retrospective (learnings, what to improve)
3. **All team:** Update architecture patterns based on Phase 1 experience

---

## The 4 Agents Explained in 1 Minute

### ML Engineer (Model Expert)
**You build:** LSTM-VAE, Isolation Forest, synthetic data pipeline  
**You own:** Model accuracy, training time, inference latency  
**Non-negotiables:** Reproducible runs, no hard-coded thresholds, metrics logged to DB  
**Done when:** Model checkpoint saved with metadata, tests pass >80%, Architect approves ADR

### Backend Engineer (Data Pipeline Expert)
**You build:** REST API, PostgreSQL schema, CSV/MQTT ingestion, database migrations  
**You own:** API latency <200ms, data validation, error handling  
**Non-negotiables:** All input validated, migrations versioned, circuit breakers for external APIs  
**Done when:** E2E test passes (CSV → DB → inference → alert), API documented, Architect approves

### DevOps/CS Lead (Infrastructure & Operations Expert)
**You build:** Terraform IaC, monitoring dashboards, backup scripts, customer runbooks  
**You own:** Uptime 99%+, RTO 1h, RPO 15m, incident response <4h  
**Non-negotiables:** Everything in Terraform (no manual changes), monitoring before customer deploy, backups tested weekly  
**Done when:** Infrastructure reproducible from code, restore tested, runbooks peer-reviewed, Architect approves

### Solution Architect (Design Overseer & Decision Advocate)
**You guard:** Architecture patterns, code quality, scalability, Phase 1.5 readiness  
**You own:** ADRs filed, no architectural debt, team decisions documented, phase progression validated  
**Non-negotiables:** Every major decision in ADR, weekly architecture sync, code reviews for debt, Phase 1.5 feasibility assessed  
**Done when:** All code reviewed, all ADRs approved, team aligned on decisions, Phase 1.5 refactor <20% effort

---

## Key Principles

### 1. Canonical Design, Modular Implementation
- **Canonical:** Single sensor schema (vendor-agnostic)
- **Modular:** LSTM-VAE separate from Isolation Forest (can be swapped)
- **Extensible:** Priva/Hoogendoorn APIs add layers without breaking canonical model

### 2. Decisions Are Documented
- **Every** major decision → ADR filed
- **Every** ADR → team review → approval before code merge
- **ADRs live in repo** (version controlled, not Slack threads)

### 3. Architecture Decisions Are Questioned
**Not adversarially, but rigorously:**
- "Why LSTM-VAE over Isolation Forest only?" (force justification)
- "Will this scale to 10 customers?" (force scalability thinking)
- "What if X fails?" (force error handling thinking)

### 4. Operations Are Automated
- **Infrastructure:** Terraform (reproducible, version-controlled)
- **Monitoring:** Prometheus + Grafana (dashboards before customer deploy)
- **Backups:** Automated + tested weekly (not just "successful")
- **Runbooks:** Documented + peer-reviewed (not tribal knowledge)

### 5. Quality Is Built In, Not Bolted On
- **Testing:** >80% coverage from day 1 (not added at the end)
- **Metrics:** Logged from day 1 (not added in Phase 2)
- **Documentation:** Written as code is committed (not retrospectively)
- **Monitoring:** Running before customer deploy (not after complaints)

---

## Red Flags Summary

### 🛑 Stop Work Immediately
1. Tight coupling between LSTM-VAE + Isolation Forest
2. Database schema that doesn't scale to 5+ customers
3. No monitoring configured
4. Model checkpoint without metadata
5. Secrets in code or config files

### 🟡 Discuss Before Merge
1. Premature optimization without profiling
2. Monolithic API endpoints
3. Silent failures (missing sensors)
4. Hard-coded thresholds
5. Code duplication

---

## Weekly Communication Template

### Tuesday 10 AM: Architecture Sync (30 min)
**Attendees:** ML, BE, DevOps, Architect  
**Format:** Standup (5 min) + Architecture Review (15 min) + Decisions (8 min) + Next Week (2 min)

**Standup:**
- ML Engineer: Model training status, any blockers?
- BE Engineer: API endpoints done, any schema questions?
- DevOps: Infrastructure ready, monitoring running?
- Architect: Code review findings, ADRs needed?

**Architecture Review:**
- New dependencies added? Justify.
- Database schema changes? Review migration.
- API contract changes? Breaking changes?
- New error modes? SLA impact?
- Model accuracy trend? Regression?
- Performance baseline hit?
- Phase 1.5 risk assessment?

**Decisions:**
- Document any decisions as ADR
- Assign action items
- Update project tracker

**Output:**
- Slack thread with summary
- ADR links if filed
- Next week priorities

---

## Success Criteria (End of Phase 1)

### Technical
- ✅ Anomaly detection 70%+ accurate (validated with customer)
- ✅ API latency p99 <200ms
- ✅ Database ingestion rate 1000+ reads/sec
- ✅ Model checkpoint saved + reproducible
- ✅ All code >80% test coverage

### Architectural
- ✅ No hard-coupled components (all modular)
- ✅ Canonical schema extensible to Priva/Hoogendoorn
- ✅ Phase 1.5 refactor <20% effort
- ✅ All decisions documented in ADRs

### Operational
- ✅ Infrastructure 100% Terraform
- ✅ Monitoring dashboards live (no customer complaints about "why did it happen?")
- ✅ Backup/restore tested weekly
- ✅ Customer onboarding runbook proven on 1 beta customer

### Team
- ✅ Weekly sync established (notes + decisions documented)
- ✅ Code review process working (PRs reviewed within 24h)
- ✅ Escalation path proven (red flags caught early)
- ✅ ADR discipline enforced (decisions documented, not Slack threads)

---

## Phase 1 → Phase 1.5 Transition Checklist

**At the end of Week 6 of Phase 1, Solution Architect runs:**

### Go/No-Go Assessment
- [ ] Can LSTM-VAE fine-tune on Phase 1.5 customer data without refactor? (>50% = blocker)
- [ ] Are all metrics logged to database (not just notebooks)?
- [ ] Is canonical schema extensible to new vendors?
- [ ] Is API backward-compatible (no breaking changes for Phase 2)?
- [ ] Is infrastructure reproducible (Terraform only, no manual steps)?
- [ ] Are runbooks tested on real customer scenario?
- [ ] Is feedback loop working (operators marking, data stored)?

### Readiness Verdict
- 🟢 **GREEN (Ready):** <10% refactor estimated → Proceed to Phase 1.5
- 🟡 **YELLOW (Hold):** 10-20% refactor estimated → Address blockers, reassess
- 🔴 **RED (Not Ready):** >20% refactor estimated → Architecture rework needed

---

## Tools & Commands (Quick Start)

### Python Project Setup
```bash
# Environment
python -m venv venv && source venv/bin/activate

# Dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Testing
pytest tests/ -v --cov=src/ --cov-report=term-missing

# Linting
black src/ && isort src/ && pylint src/

# Type checking
mypy src/
```

### Infrastructure Setup
```bash
# Terraform
terraform init
terraform plan
terraform apply

# Monitoring
docker run -d -p 9090:9090 prom/prometheus
docker run -d -p 3000:3000 grafana/grafana

# Database
docker run -d --name postgres -e POSTGRES_PASSWORD=dev postgres:15
alembic upgrade head
```

### Git Workflow
```bash
# Branch naming
git checkout -b feature/lstm-vae-training
git checkout -b fix/api-validation
git checkout -b docs/adr-001

# Commit message format
git commit -m "feat: implement LSTM-VAE model architecture

- Add encoder-decoder layers
- Implement reconstruction loss
- Add unit tests for forward pass

Fixes #123
Related-To: ADR-001"

# PR: Assign to Architect for review
# Squash merge after approval
```

---

## Common Questions

### Q: What if we disagree on architecture?
**A:** File an ADR with your position, architect reviews, team discusses Tuesday sync, document decision. No decision is final until ADR is approved.

### Q: What if something is urgent?
**A:** Slack thread with @architect tag, include context + what you've tried. <1h response time. But still file ADR after fix.

### Q: Can I skip tests?
**A:** No. >80% coverage is non-negotiable. If code is hard to test, it's probably poorly designed.

### Q: Can I commit directly to main?
**A:** No. All PRs reviewed by Architect. Feature branches only.

### Q: What if Phase 1.5 needs major refactoring?
**A:** That's a blocker. Escalate to Architect + Product immediately. You likely have architectural debt that needs fixing before Phase 1 ends.

### Q: How do I know if my code is "done"?
**A:** Check your role's "Definition of Done" checklist. If every box is checked + Architect approved, you're done.

---

## Next Steps

### Week 0 (This Week)
1. **All team:** Read Agent_Team_Reference_Guide.md (focus on your role)
2. **All team:** Bookmark Team_Quick_Reference.md
3. **Architect:** Start ADR-001 (LSTM-VAE vs Isolation Forest)
4. **Architect:** Schedule Tuesday sync for Week 1
5. **All team:** Confirm understanding of escalation path

### Week 1
1. **All team:** First Tuesday architecture sync
2. **Architect:** Finalize & approve ADR-001 through ADR-005
3. **All team:** Project kickoff — set up development environments
4. **ML Engineer:** Start synthetic data generation
5. **BE Engineer:** Start canonical schema design
6. **DevOps:** Start Terraform infrastructure code

### Week 2+
1. **All team:** Weekly Tuesday sync (non-negotiable)
2. **All team:** Code reviews within 24 hours
3. **All team:** Track metrics (accuracy, latency, coverage)
4. **Architect:** Flag red flags as they appear

---

## Document Status

- ✅ **Interactive Team Overview:** Ready (widget)
- ✅ **Agent Team Reference Guide:** Ready (Agent_Team_Reference_Guide.md)
- ✅ **Team Quick Reference:** Ready (Team_Quick_Reference.md)
- ✅ **This Summary:** Ready (this document)

**Total documentation:** ~2,000 lines covering roles, decisions, workflows, guardrails, red flags

**Designed for:** 4 person team (ML, BE, DevOps, Architect) building Phase 1 MVP

**Time to onboard:** 2-3 hours reading (engineer reads their role section + quick reference)

---

**Good luck building the AI Agronomist platform!**  
**- Your Solution Architect**

---

*Last updated: May 13, 2026*  
*Questions? Tag @architect in Slack*  
*Ready to start Phase 1? See you Tuesday!*

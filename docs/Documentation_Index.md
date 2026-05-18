# AI Agronomist Phase 1 — Agent Team Documentation Index

**Status:** Complete & ready for implementation  
**Date:** May 13, 2026  
**Team:** 4 agents + 1 architect oversight  

---

## 📚 Documentation Overview

This package contains **~2,000 lines** of documentation covering team structure, guardrails, workflows, coding standards, and decision-making frameworks for Phase 1 implementation.

### What You Need to Know Right Now

1. **4 agents** (ML, BE, DevOps, Architect) with clearly defined roles, responsibilities, and non-negotiables
2. **Weekly architecture sync** (Tuesday 10 AM) as the team's decision-making forum
3. **ADR discipline** — every major decision documented (context, decision, rationale, consequences, alternatives)
4. **Red flags** that trigger immediate escalation (tight coupling, no monitoring, secrets in code, etc.)
5. **Definition of Done** per role (what "complete" looks like for each agent)

---

## 📖 Documents & Artifacts

### 1. Agent_Team_Reference_Guide.md
**Use this for:** Comprehensive understanding of roles, guardrails, coding standards

**Contains:**
- Agent personas (ML, BE, DevOps, Architect)
  - Profile, responsibilities, expected outputs
  - Core skills & skillsets
  - Guardrails (non-negotiables)
  - Definition of Done (completion criteria)
  - Code quality standards per role
  - Anti-patterns to avoid
- Guardrails & constraints (Phase 1-wide)
- Coding standards (Python, testing, logging)
- Decision workflows (with worked examples)
- Architecture Decision Records (ADR) template
- Weekly sync & review checklist
- Escalation & red flags matrix

**Read this if:** You're a team member and want to understand your role deeply, or you're the Architect and need to know what you're overseeing

**Time to read:** 30-45 min (or skim your role section: 10 min)

---

### 2. Team_Quick_Reference.md
**Use this for:** Day-to-day decision-making and operations

**Contains:**
- Decision matrix (who decides what: code, architecture, operations)
- Escalation path (Tier 1-4 criteria)
- Communication protocol (sync vs. async, channels, response times)
- Definition of Done (runbook format for each role)
- Red flags checklist (P1=stop, P2=discuss, P3=no action)
- Suggested sprint structure
- Key metrics to track
- Emergency contact protocol

**Read this if:** You need to know "Should I escalate this?" or "What does done look like for my code?"

**Time to read:** 10-15 min (or bookmark and refer to specific sections)

---

### 3. Agent_Team_Setup_Summary.md
**Use this for:** Executive overview, onboarding, FAQ

**Contains:**
- What you have (document overview)
- How to use these documents (by role)
- 4 agents in 1 minute each
- Key principles (canonical design, modular implementation, documented decisions)
- Red flags summary
- Weekly communication template
- Success criteria (end of Phase 1)
- Phase 1→1.5 transition checklist
- Tools & commands (quick start)
- Common questions & answers
- Next steps

**Read this if:** You're onboarding or need a quick refresher on the team structure

**Time to read:** 15 min

---

### 4. Interactive Team Overview Widget (Embedded in Chat)
**Use this for:** Visual understanding of team structure and oversight

**Contains (3 tabs):**
- **Team Overview:** All 4 agents with skills, guardrails, DoD per role
- **Architect Role:** Solution Architect role explained, weekly checklist, red flags caught
- **Decision Workflows:** How decisions flow (with examples)

**Read this if:** You're visual or new to the team

**Time to view:** 5 min

---

### 5. Interactive Team Organization Diagram (Embedded in Chat)
**Use this for:** Understanding oversight model and communication flows

**Contains (3 tabs):**
- **Team Structure:** Org chart, who reports to whom
- **Oversight Model:** How architect provides oversight, code review flow, decision flow
- **Weekly Sync:** Detailed agenda, what gets reviewed

**Read this if:** You want to understand how decisions get made

**Time to view:** 5 min

---

## 🎯 How to Use (by Role)

### 👨‍💻 ML Engineer
1. Read: **Agent_Team_Reference_Guide.md** → "ML Engineer" section (10 min)
2. Bookmark: **Team_Quick_Reference.md** → Search for "ML Engineer" when needed
3. Know by heart: Definition of Done for your code (8 checkboxes)
4. Understand: Red flags (🛑 stop work items + 🟡 discuss before merge)
5. Attend: Weekly Tuesday sync (non-negotiable)

**Key documents:** Reference Guide (your role), Quick Reference, ADR template

**Your responsibilities:**
- LSTM-VAE design, training, validation
- Synthetic data generation
- Model metric logging to database
- Reproducible runs (fixed random seed)
- >80% test coverage on model code

**Your non-negotiables:**
- No pre-trained models without validation metrics
- No magic number hyperparameters (justify each)
- Synthetic data assumptions documented
- Model checkpoint must work in Phase 1.5 fine-tuning

---

### 🏗️ Backend Engineer
1. Read: **Agent_Team_Reference_Guide.md** → "Backend Engineer" section (10 min)
2. Bookmark: **Team_Quick_Reference.md** → Search for "Backend Engineer" when needed
3. Know by heart: Definition of Done for your code (8 checkboxes)
4. Understand: Red flags + anti-patterns (monolithic APIs, no error handling, hard-coded values)
5. Attend: Weekly Tuesday sync

**Key documents:** Reference Guide (your role), Quick Reference, ADR template

**Your responsibilities:**
- REST API design (FastAPI)
- CSV/MQTT/REST data ingestion
- PostgreSQL + TimescaleDB schema + migrations
- Data validation + error handling
- Feedback loop API

**Your non-negotiables:**
- All API endpoints validate input (Pydantic)
- Database migrations are versioned (Alembic)
- Every API call has timeout + retry logic + circuit breaker
- Sensor data never silently discarded (log all issues)
- Schema is backward-compatible (no breaking changes)

---

### ⚙️ DevOps/CS Lead
1. Read: **Agent_Team_Reference_Guide.md** → "DevOps/CS Lead" section (10 min)
2. Bookmark: **Team_Quick_Reference.md** → Search for "DevOps" when needed
3. Know by heart: Definition of Done for infrastructure (8 checkboxes)
4. Understand: Red flags (no monitoring, no tested backups, secrets in code)
5. Attend: Weekly Tuesday sync

**Key documents:** Reference Guide (your role), Quick Reference

**Your responsibilities:**
- Infrastructure as Code (Terraform)
- PostgreSQL + TimescaleDB setup & backups
- Mosquitto MQTT broker
- Prometheus + Grafana monitoring
- Alerting + incident response
- Customer onboarding runbooks

**Your non-negotiables:**
- All infrastructure in Terraform (no manual SSH changes)
- Monitoring alerts configured BEFORE customer deploy
- Daily backup verification (weekly test restore)
- Customer credentials rotated monthly
- Performance baselines tracked from day 1

---

### 🏛️ Solution Architect
1. Read: **Agent_Team_Reference_Guide.md** → entire document (45 min)
2. Read: **Team_Quick_Reference.md** → focus on "Decision Matrix" + "Escalation Path" (10 min)
3. Read: **Agent_Team_Setup_Summary.md** (15 min)
4. Understand: Red flags matrix (your job to catch these)
5. Prepare: ADR template + sample ADRs for Week 1
6. Lead: Weekly Tuesday sync (non-negotiable)

**Key documents:** All of them (you oversee the entire team)

**Your responsibilities:**
- Architecture pattern definition
- Code reviews for architectural debt (not just bugs)
- Decision documentation (ADRs)
- Weekly architecture sync leadership
- Phase 1.5 readiness assessment
- Challenging assumptions ("Why? Will it scale?")

**Your non-negotiables:**
- Every major decision → ADR (filed before code merge)
- Weekly sync happens every Tuesday (structured agenda)
- Code review for architectural soundness (not just correctness)
- Phase 1.5 migration path validated (must be <20% refactor)
- All red flags escalated immediately

---

## 🚀 Getting Started (Week 0)

### Day 1: Onboarding
- [ ] All team members: Read Agent_Team_Setup_Summary.md (15 min)
- [ ] All team members: Bookmark Team_Quick_Reference.md
- [ ] All team members: View Interactive Team Widgets (5 min)

### Day 2: Deep Dive by Role
- [ ] ML Engineer: Read your section in Agent_Team_Reference_Guide.md (10 min)
- [ ] BE Engineer: Read your section in Agent_Team_Reference_Guide.md (10 min)
- [ ] DevOps: Read your section in Agent_Team_Reference_Guide.md (10 min)
- [ ] Architect: Read full Agent_Team_Reference_Guide.md (45 min)

### Day 3: Preparation
- [ ] All team: Confirm understanding of red flags + escalation path
- [ ] All team: Install dev tools (Python, Terraform, pytest, etc.)
- [ ] Architect: Prepare ADR-001 through ADR-005 drafts
- [ ] Architect: Set up project tracker for Phase 1

### Day 4: Kickoff Planning
- [ ] All team: Confirm Tuesday 10 AM sync (first meeting: Week 1)
- [ ] All team: Review Week 1 sprint plan (provided separately)
- [ ] All team: Set up communication channels (Slack, GitHub, etc.)

---

## 📋 Reference Sections (Quick Links)

### Decision Making
- **Decision Matrix** (Team_Quick_Reference.md) → "Who decides what?"
- **Escalation Path** (Team_Quick_Reference.md) → "When to escalate?"
- **Decision Workflows** (Agent_Team_Reference_Guide.md) → Worked examples
- **ADR Template** (Agent_Team_Reference_Guide.md) → How to document decisions

### Red Flags & Quality
- **Red Flags Checklist** (Team_Quick_Reference.md) → P1/P2/P3 criteria
- **Anti-Patterns** (Agent_Team_Reference_Guide.md, per role) → What to avoid
- **Definition of Done** (Team_Quick_Reference.md, per role) → Completion criteria
- **Code Quality Standards** (Agent_Team_Reference_Guide.md, per role) → Testing, type hints, etc.

### Communication & Operations
- **Communication Protocol** (Team_Quick_Reference.md) → Sync vs. async
- **Weekly Sync Structure** (Agent_Team_Reference_Guide.md) → Agenda template
- **Sprint Structure** (Team_Quick_Reference.md) → Week-by-week plan
- **Emergency Contact Protocol** (Team_Quick_Reference.md) → P1/P2/P3 response

### Phase 1 Success
- **Success Criteria** (Agent_Team_Setup_Summary.md) → Technical, architectural, operational, team
- **Phase 1→1.5 Transition** (Agent_Team_Setup_Summary.md) → Readiness assessment
- **Key Metrics** (Team_Quick_Reference.md) → What each role tracks

---

## 🔍 Finding What You Need

**Question:** "What does 'done' look like for my code?"  
**Answer:** Team_Quick_Reference.md → Find your role → "Definition of Done" section

**Question:** "Should I escalate this issue?"  
**Answer:** Team_Quick_Reference.md → "Escalation Path" (Tier 1-4 criteria)

**Question:** "How do I handle this architectural decision?"  
**Answer:** Agent_Team_Reference_Guide.md → "Decision Workflows" (worked examples)

**Question:** "Is this a red flag?"  
**Answer:** Team_Quick_Reference.md → "Red Flags Checklist" (P1/P2/P3)

**Question:** "What are my responsibilities?"  
**Answer:** Agent_Team_Reference_Guide.md → Find your role section

**Question:** "How do we communicate?"  
**Answer:** Team_Quick_Reference.md → "Communication Protocol"

**Question:** "What gets documented?"  
**Answer:** Agent_Team_Reference_Guide.md → "Architecture Decision Records (ADR)"

---

## 📊 Document Statistics

| Document | Lines | Purpose | Read Time |
|----------|-------|---------|-----------|
| Agent_Team_Reference_Guide.md | 800+ | Comprehensive role guide | 45 min |
| Team_Quick_Reference.md | 600+ | Day-to-day operations | 15 min |
| Agent_Team_Setup_Summary.md | 400+ | Executive overview | 15 min |
| Interactive Team Widgets | 2 | Visual dashboards | 5 min |
| **Total** | **1,800+** | **Complete framework** | **90 min** |

---

## ✅ Verification Checklist

Before Phase 1 starts, confirm:

- [ ] All team members have read Agent_Team_Setup_Summary.md
- [ ] All team members understand their role (read role section in Reference Guide)
- [ ] All team members know the red flags (can name 3 P1 flags from memory)
- [ ] All team members know the escalation path (Tier 1, 2, 3, 4)
- [ ] Architect has prepared ADR-001 through ADR-005
- [ ] Tuesday 10 AM sync scheduled for Week 1
- [ ] Dev environment setup plan in place
- [ ] Team has confirmed understanding of "Definition of Done"

---

## 🎯 The Intent

This documentation is **not** a restriction. It's a **guardrail** — clear guidance so the team:
- Makes decisions **together** (Tuesday sync, not individual)
- Documents decisions **always** (ADRs, not Slack threads)
- Avoids **common mistakes** (red flags, anti-patterns)
- Ships **quality** (>80% tests, monitoring from day 1)
- Scales **cleanly** (modular design, Phase 1.5 in view)

The framework is **flexible** — update it as you learn. ADRs captured decisions, retrospectives surface improvements, and you're encouraged to challenge assumptions.

**Expect to evolve this.** Phase 1 will teach you what works. Use these documents as a foundation, not a straitjacket.

---

## 📞 Support

**Questions about:** 
- Your role → Read your section in Agent_Team_Reference_Guide.md
- Decisions → Team Quick Reference (Decision Matrix)
- Red flags → Team Quick Reference (Red Flags Checklist)
- Operations → Team Quick Reference (Communication Protocol)
- ADRs → Agent_Team_Reference_Guide.md (ADR Template)
- Overall strategy → Agent_Team_Setup_Summary.md (Key Principles)

**Unclear?** Post in #architecture Slack channel, tag @architect

**Blocker?** Use escalation path (Team_Quick_Reference.md) to determine if Tier 2/3/4

---

**Status:** Ready for Phase 1 implementation  
**Last updated:** May 13, 2026  
**Owner:** Solution Architect

---

*Welcome to the team. Let's build something great.* 🚀

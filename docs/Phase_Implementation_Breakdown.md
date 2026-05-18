# AI Agronomist Platform — Detailed Phase Implementation Breakdown

**Created from:** Final_Implementation_Guide_Progression.md  
**Date:** May 12, 2026  
**Status:** Ready for sprint planning

---

## Executive Overview

| Phase | Duration | Focus | Team Size | Customers | Success KPI |
|-------|----------|-------|-----------|-----------|-------------|
| **Phase 1 (MVP)** | 3 months | Prove anomaly detection | 2-3 | 1 (beta) | 70%+ marking accuracy |
| **Phase 1.5** | 4-6 weeks | Scalable model adaptation | 1-2 | 2-5 | 50%+ false positive reduction |
| **Phase 2** | 4-6 months | Multi-vendor scale + LLM | 4-5 | 5+ | 60%+ portal engagement |
| **Phase 3** | 3-4 months | Self-learning recommendations | 4-5 | 10+ | 85%+ LLM confidence |
| **Phase 4** | 3-4 months | Autonomous control | 4-5 | 10+ | 90%+ autonomous success |

---

# PHASE 1: MVP — AI Agronomist (3 Months)

## Strategic Goals
- **Primary:** Prove anomaly detection works with real field data from 1 beta customer
- **Secondary:** Validate operator feedback loop; build reusable training content
- **Outcome:** Repeatable MVP pattern for Phase 1.5 scale

## Team Composition
- 1 ML engineer (primary owner)
- 1 backend engineer (data pipeline + infrastructure)
- 1 DevOps/CS lead (optional, can be part-time)

**Total person-months:** 3–3.5

## Architecture & Data Flow

```
Beta Customer (Priva system)
    ↓
MQTT Bridge / REST API adapter
    ↓
PostgreSQL + TimescaleDB
    ↓
LSTM-VAE (trained on synthetic data)
    ↓
Isolation Forest (secondary validation)
    ↓
Correlation Engine (physical ratios: CO₂/temp, humidity/heating, etc.)
    ↓
Streamlit Dashboard (anomaly scores + alerts)
    ↓
Operator Feedback (marks real/false)
    ↓
Weekly Feedback Loop (thresholds adjusted manually)
```

## Technical Deliverables (Checklist)

### Week 1-2: Foundation
- [ ] **Canonical Sensor Schema**
  - Vendor-agnostic data model (temperature, humidity, CO₂, NOx, power, water, etc.)
  - Documentation: field definitions, units, expected ranges
  - Version control in git

- [ ] **Customer Data Ingestion**
  - CSV parser (fallback for manual uploads)
  - MQTT subscriber (Priva → MQTT bridge, if available)
  - REST API adapter (Priva cloud API)
  - Data validation + sanitization
  - Error logging + monitoring

### Week 3-4: Core Anomaly Detection
- [ ] **Synthetic Data Pipeline**
  - TimeGAN or similar to generate baseline patterns
  - 2–4 weeks of synthetic data per seasonal profile
  - Document assumptions (normal patterns, parameter ranges)

- [ ] **LSTM-VAE Model**
  - Architecture: encoder (input → latent), decoder (latent → reconstruction)
  - Train on synthetic baseline data
  - Metrics: reconstruction error, latent space density
  - Model checkpoint saved (for Phase 1.5 fine-tuning)

- [ ] **Isolation Forest (Ensemble)**
  - Unsupervised anomaly detector (physical ratio violations)
  - Train on synthetic data (same as LSTM-VAE)
  - Rules: CO₂ supply failure (low CO₂ + normal sensors), NOx contamination (CO₂ low but sensors normal), etc.

### Week 5-6: Correlation & Dashboard
- [ ] **Correlation Engine**
  - Physical ratio monitoring (e.g., humidity rise without heating = failed ventilation)
  - Causal graph (boiler failure → heat drop → humidity rise)
  - Alert scoring (confidence % based on multiple signals)

- [ ] **Streamlit Dashboard (MVP)**
  - Real-time anomaly scores
  - Alert history + filter by type
  - Operator feedback buttons (real/false/investigating)
  - Chart: anomaly score over time
  - Manual threshold adjustment UI

### Week 7-8: Infrastructure & Deployment
- [ ] **PostgreSQL + Mosquitto MQTT Broker**
  - Schema: sensor_readings, anomalies, feedback, models
  - TimescaleDB hypertables for time-series compression
  - MQTT topic structure (standardized)
  - Backup strategy

- [ ] **Monitoring & Logging**
  - Alert when no new sensor data (ingestion failure)
  - Model inference latency tracking
  - Feedback loop health (operator response rate)
  - Daily summary email (anomalies detected, feedback received)

- [ ] **Beta Customer Deployment**
  - Connect to Priva system (or CSV upload if API unavailable)
  - Database seeding (2-4 weeks of historical data)
  - Dashboard access (operator account setup)
  - Automated daily inference (new sensor data → anomaly scores)

## Operator Training Plan (Phase 1)

### Timeline & Content

**Week 1 (Deployment Week):**
```
Monday:
  - Schedule 30-min video call with greenhouse operator
  - Send pre-call email: "Overview of what we'll discuss"

Wednesday-Thursday:
  - 30-min live video call (60–75% of Phase 1 training investment)
  
Call structure:
  ├─ 5 min:  Welcome + system overview ("learns your greenhouse patterns")
  ├─ 10 min: Show 3 real anomaly examples from similar greenhouses
  │          (explain physical reason for each)
  ├─ 10 min: Show 3 false positive examples (why they're safe to ignore)
  ├─ 3 min:  How to mark alerts in dashboard
  └─ 2 min:  Feedback loop (your marks → system improvement)

After call:
  - Within 1 hour: Send recorded video (reuse forever)
  - Send one-page laminated decision guide: "Is this alert real?"
  - Add operator to direct Slack channel (you + operator)
```

**Week 2-8 (Running Live):**
```
Daily:
  - Operator marks alerts as they come in (target: 80%+ marked within 24h)
  
Weekly (Friday):
  - 15-min async check-in (Slack or email)
    "Marking quality looks good. Here's what improved this week."
  - If issues: offer brief clarification call
  
After 4 weeks:
  - 1 optional follow-up call (30 min) if operator wants deeper training
```

### Success Criteria (Phase 1 Training)
- ✅ Operator marks 80%+ of alerts within 24 hours
- ✅ Marking accuracy 70%+ (verified against field reality)
- ✅ Operator confidence building (willing to give detailed feedback)
- ✅ Operator understands definition of "real anomaly" vs. noise
- ✅ Video + decision guide ready for reuse in Phase 1.5

### Training Artifacts Produced
1. **30-min recorded video** (YouTube private link, embedded later)
2. **One-page decision guide** (laminated, mailed to operator)
3. **Email template for weekly check-ins**
4. **Slack channel guidelines** (how to ask questions, response time)

## Customer Support Plan (Phase 1)
- **Response time:** 24 hours (typically 4-8 hours)
- **Channel:** Direct Slack (not ticketing system yet)
- **Escalation:** You personally (small team)
- **Common issues to anticipate:**
  - "Why did the system alert on X?" (explain physical reason)
  - "Can I ignore this alert?" (clarify false positive process)
  - "Dashboard not loading" (connection troubleshooting)

## Phase 1 Success Metrics
- ✅ Anomaly detection validated (70%+ operator agreement)
- ✅ 1 beta customer running for 8+ weeks
- ✅ Operator feedback quality enabling weekly threshold refinement
- ✅ 0 critical production outages (>4 hours downtime)
- ✅ Reusable training content ready (video + guide)
- ✅ Clear picture of feedback loop (how often operators mark, accuracy trends)

## Phase 1 Deliverables Summary
```
Engineering:
  ├─ Canonical schema (1 doc)
  ├─ Data ingestion pipeline (CSV + MQTT + REST)
  ├─ LSTM-VAE model (checkpoint + code)
  ├─ Isolation Forest (ensemble baseline)
  ├─ Correlation engine (physical ratio rules)
  ├─ PostgreSQL + TimescaleDB setup
  ├─ Mosquitto MQTT broker
  ├─ Streamlit dashboard
  ├─ Monitoring + alerting
  └─ 1 deployed customer

Training & Operations:
  ├─ 30-min recorded video
  ├─ One-page decision guide
  ├─ Weekly check-in email template
  └─ Slack support channel

Documentation:
  ├─ Architecture diagram (data flow)
  ├─ Model training guide (how to retrain)
  ├─ Customer onboarding runbook
  └─ Troubleshooting guide
```

## Phase 1 Risk Mitigation
| Risk | Mitigation |
|------|-----------|
| Priva API unavailable | Fall back to CSV upload (manual, but works) |
| Synthetic data unrealistic | Use customer's first 2 weeks to retrain live |
| False positive storm | Lower thresholds manually, improve Isolation Forest rules |
| Operator burnout (marking) | Reduce to 50% of alerts after week 2, focus on quality |
| No clear signal in data | Interview operator: "What failures happened this month?" |

## Phase 1 Go/No-Go Criteria (End of Week 8)
- ✅ **GO:** 70%+ marking accuracy, operator satisfied, clear signal for Phase 1.5
- ⚠️ **HOLD:** 50–70% accuracy, need more customer feedback, extend 2 more weeks
- ❌ **STOP:** <50% accuracy, fundamental model issue, pivot to Phase 1.5 infrastructure only

---

# PHASE 1.5: Model Adaptation Infrastructure (4-6 Weeks)

## Strategic Goals
- **Primary:** Build scalable infrastructure so ANY new greenhouse can auto-tune the model in parallel
- **Secondary:** Onboard 2-5 customers; prove model transfer learning works
- **Outcome:** Repeatable onboarding automation for Phase 2 scale

## Team Composition
- 1 ML engineer (transfer learning + tuning pipeline)
- 1 backend engineer (automation, deployment)
- Optional: 0.5 FTE from Phase 1 backend engineer

**Total person-months:** 1.5–2

## Architecture & Data Flow

```
Phase 1 Baseline
  ├─ LSTM-VAE (pre-trained on synthetic data + Phase 1 customer)
  └─ Isolation Forest rules
      ↓
New Customer (Customer B, C, D, E)
  ├─ CSV Upload (2-4 weeks known-good data)
  ├─ Seasonal Profile Auto-Detection (spring/summer/autumn/winter)
  └─ Location (Netherlands/Spain/Scandinavia for climate adaptation)
      ↓
Baseline Analysis Engine
  ├─ Extract patterns from CSV
  ├─ Validate data quality
  └─ Generate customer-specific baseline profile
      ↓
Transfer Learning (LSTM-VAE Fine-Tuning)
  ├─ Fine-tune on customer data (30–60 min on CPU)
  ├─ Threshold recalibration per customer
  └─ Model registry creates Version 1.0
      ↓
Deployment
  ├─ Load customer-specific model
  ├─ Send introductory email
  └─ Start daily inference
      ↓
Operator Feedback Loop
  ├─ Operator marks alerts (real/false)
  ├─ Weekly auto-adjustment (thresholds refined from feedback)
  └─ Model registry tracks versions
```

## Technical Deliverables (Checklist)

### Week 1-2: Baseline Analysis Engine
- [ ] **Baseline Extraction Service**
  - Accepts CSV (2-4 weeks of sensor data)
  - Validates: no NaNs, correct sensors, reasonable ranges
  - Calculates: mean, std, min/max per sensor
  - Extracts: daily/hourly patterns (min/max by hour of day)
  - Output: JSON baseline profile

- [ ] **Seasonal Profile Library**
  - 4 baseline profiles per location (spring/summer/autumn/winter)
  - Keyed by: location (lat/long or region) + crop type
  - Stored in: PostgreSQL or model registry
  - Manual override: customer can select season + location

- [ ] **Data Quality Checks**
  - Detect gaps (>24h missing data = invalid)
  - Detect outliers (>5σ = potential failure to learn from)
  - Flag for review (email to operator: "Your data looks unusual here")

### Week 2-3: Transfer Learning Pipeline
- [ ] **LSTM-VAE Fine-Tuning**
  - Load Phase 1 pre-trained model
  - Fine-tune on customer data (30–60 min, CPU-bound)
  - Early stopping: stop if validation loss plateaus
  - Save: customer-specific checkpoint

- [ ] **Threshold Recalibration**
  - Calculate reconstruction error distribution on customer data
  - Auto-set anomaly threshold (e.g., 95th percentile)
  - Isolation Forest rule adjustment (per-customer parameters)
  - Document: why thresholds chosen for this customer

- [ ] **Model Registry**
  - Version control: each customer model gets ID + version
  - Metadata: customer name, location, crop, created date, accuracy metrics
  - Rollback capability: can revert to prior version if new version bad
  - Storage: model files + JSON metadata

### Week 3-4: Automated Deployment & Onboarding
- [ ] **Onboarding Workflow Automation**
  - Step 1: Customer uploads CSV via web form
  - Step 2: Baseline analysis + fine-tuning (happens in background)
  - Step 3: Notify operator via email ("Your model is ready!")
  - Step 4: Deploy customer-specific model
  - Step 5: Send training email + video link

- [ ] **Email Automation**
  - Introductory email template ("Your greenhouse is unique. We've learned its patterns.")
  - Training email with 5-min video link
  - Weekly feedback summary ("You marked 47 alerts, accuracy 75%")
  - Monthly improvement email ("False positives dropped 40% this month")

- [ ] **Dashboard Personalization**
  - Customer-specific anomaly thresholds
  - Seasonal baseline displayed (so operator knows which was auto-selected)
  - Customer's own recent alerts (not generic examples)

### Week 4-5: Training Content & Portal Prep
- [ ] **Reusable Training Content**
  - 5-min video (from Phase 1, edited for general audience)
  - One-page decision guide (laminated, ready for mail)
  - Email series template (week 1-3 emails drafted)

- [ ] **Portal Infrastructure (Foundation)**
  - Video hosting (YouTube unlisted, embedded in app)
  - Decision guide PDF stored + delivery pipeline
  - Email send service configured (SendGrid, Mailgun)
  - Slack integration (for support channel auto-invites)

### Week 5-6: Onboard 2-5 Customers & Iterate
- [ ] **Customer B, C, D, E Setup**
  - Each customer: upload CSV, auto-tune, deploy, train
  - Diverse mix: different vendors (Priva, Hoogendoorn), crops, regions
  - Document any unique challenges

- [ ] **Model Performance Tracking**
  - Weekly: compare customer model accuracy vs. baseline model
  - Document: which customers improved most from fine-tuning
  - Iterate: if one customer's model underperforms, diagnose

- [ ] **Training Success Metrics**
  - Track: % of operators watching 5-min video
  - Track: % of operators downloading decision guide
  - Track: alert response time (time from alert → operator mark)

## Operator Training Plan (Phase 1.5)

### Tiered Approach
**Tier 1: All Operators (Automatic)**
```
Step 1: Onboarding (When deployed)
  ├─ Introductory email ("Welcome to AI Agronomist")
  ├─ 5-min video link (YouTube unlisted)
  ├─ One-page decision guide (PDF download + postal mail option)
  └─ Slack channel invite (direct support)

Step 2: Week 1 Email
  Subject: "How to mark alerts (and why it helps)"
  Content:
    - 2-min overview of feedback loop
    - 3 screenshots of dashboard marking buttons
    - "Your marks improve accuracy weekly"

Step 3: Week 2 Email
  Subject: "Top 3 mistakes operators make (don't worry, easy to fix)"
  Content:
    - Common false positives to ignore
    - Common real anomalies that matter
    - "You're doing great!"

Step 4: Week 3+ Email
  Subject: "How your feedback helped this week"
  Content:
    - Personalized: "You marked 47 alerts"
    - "Accuracy improved from 65% to 75%"
    - "Here's what you caught this week: [anomaly type]"
    - "Keep going!"

Step 5: Ongoing Support
  ├─ Slack channel (ask questions anytime)
  └─ Response time: 24 hours (typically 4-8 hours)
```

**Tier 2: Optional for Engaged Operators (Offered Week 3+)**
```
Eligibility: Operators who marked 30+ alerts in first 2 weeks

What They Get:
  ├─ 30-min live video call (your team + operator)
  ├─ Show operator's own greenhouse anomaly examples
  ├─ Practice marking real examples from their site
  ├─ Q&A: deep dives into how system works
  ├─ "Advanced Feedback Curator" certificate (digital badge)
  ├─ Monthly webinar with other advanced operators
  └─ Priority support (dedicated Slack channel)

Promotion Email (Week 3):
  Subject: "You're a natural! Want to go deeper?"
  Content:
    - Personalized: "You've marked 47 alerts with 78% accuracy"
    - Offer: 30-min optional call with our ML team
    - "Learn how to spot anomalies before they appear"
    - CTA: "Yes, I'm interested" (button)
```

### Success Criteria (Phase 1.5 Training)
- ✅ 80%+ of operators watch 5-min video (tracked via YouTube unlisted video link)
- ✅ 40-50% of operators take optional Tier 2 training (30-min call)
- ✅ Operator satisfaction 4.0+/5.0 (NPS survey after Tier 2 call)
- ✅ Zero support escalations (all questions answered in Slack)

## Phase 1.5 Success Metrics
- ✅ 2-5 customers successfully onboarded
- ✅ Baseline analysis accurate (correctly identifies customer's normal patterns)
- ✅ Fine-tuning works (customer model outperforms generic model)
- ✅ False positive rate drops 50%+ after fine-tuning
- ✅ Model improves weekly from operator feedback (thresholds trending down)
- ✅ 80%+ of operators watch training video
- ✅ 40-50% of operators take optional Tier 2 training
- ✅ Zero critical production issues in any customer

## Phase 1.5 Deliverables Summary
```
Engineering:
  ├─ Baseline analysis engine (CSV → profile)
  ├─ LSTM-VAE fine-tuning service
  ├─ Threshold recalibration logic
  ├─ Model registry (version control)
  ├─ Automated onboarding workflow
  ├─ Email automation (SendGrid integration)
  ├─ 2-5 deployed customers
  └─ Monitoring dashboard (customer-level metrics)

Training & Operations:
  ├─ 5-min video (reused from Phase 1, edited)
  ├─ One-page decision guide (finalized)
  ├─ Email series (week 1-3 templates)
  ├─ Tier 2 offer email + call template
  └─ Slack support playbook (common questions)

Documentation:
  ├─ Onboarding runbook (step-by-step)
  ├─ Baseline analysis guide (how to validate CSV)
  ├─ Transfer learning guide (fine-tuning parameters)
  ├─ Troubleshooting (model underperformance)
  └─ Customer success template (weekly check-in email)
```

## Phase 1.5 Go/No-Go Criteria (End of Week 6)
- ✅ **GO:** 2-5 customers deployed, >50% false positive reduction, ready for Phase 2
- ⚠️ **HOLD:** 1-2 customers deployed, need more validation, extend 2 more weeks
- ❌ **STOP:** Model transfer learning not working, fundamental architecture issue, pivot to simpler approach

---

# PHASE 2: Multi-Customer Scale + LLM Learning (4-6 Months)

## Strategic Goals
- **Primary:** Deploy to 5+ customers with different SCADA vendors; LLM learns from incidents
- **Secondary:** Build self-service training portal; establish operator community
- **Outcome:** Sustainable scaling model for 50+ operators; LLM improving weekly

## Team Composition
- 1 ML engineer (LLM + incident learning)
- 1 backend engineer (multi-customer infrastructure)
- 1 frontend engineer (React dashboard + portal)
- 1 customer success manager (onboarding, training, support)
- 0.5 FTE content creator (videos, case studies, forum moderation)

**Total person-months:** 18–22

## Architecture Evolution

```
Phase 1.5 (Proven):
  ├─ LSTM-VAE (per-customer fine-tuned)
  ├─ Isolation Forest
  ├─ Streamlit MVP dashboard
  └─ Email-based training

Phase 2 Additions:
  ├─ Multi-vendor SCADA API drivers (Priva, Hoogendoorn)
  ├─ React production dashboard (replaces Streamlit)
  ├─ LLM Reasoning Agent (Claude API)
  ├─ Incident database (pgvector + vector search)
  ├─ Self-service portal (videos, scenarios, forum)
  ├─ Multi-site view (10+ customers on one screen)
  ├─ Video monitoring (cameras from Phase 2 roadmap)
  └─ Analytics + reporting
```

## Technical Deliverables (Checklist)

### Week 1-4: Self-Service Portal (Training Foundation)
- [ ] **Portal Infrastructure**
  - Web app (Next.js or similar)
  - User authentication (SSO preferred, local fallback)
  - Video library (YouTube embedded, unlisted)
  - Scenario library (interactive quizzes)
  - Community forum (moderated Slack or custom)
  - Email integration (SendGrid for digests)

- [ ] **Video Content Library** (8-10 videos, 60-80 min total)
  - "Quick start: How to mark alerts" (5 min) — reuse Phase 1.5
  - "How the system learns from you" (5 min)
  - Per-failure-type videos (3-4 min each):
    - "Spotting NOx contamination" (signs, physical reason)
    - "Boiler degradation patterns" (slow heat loss)
    - "Power supply issues" (sudden sensor drop)
    - "Actuator sticking/drift" (stuck valve symptoms)
    - "CO₂ supply problems" (CO₂ won't rise)
    - "External air quality issues" (outdoor pollution)
    - "Humidity management failures" (condensation, disease risk)
    - "Temperature control failures" (overshoot, lag)
  - Real case studies (5-10 min each):
    - "How feedback helped detect NOx at Greenhouse X"
    - "Boiler failure caught 48h early" (saves €5k)
  - FAQ videos (2-3 min each):
    - "Why did the system alert?"
    - "Is this a false positive?"
    - "How do I mark accurately?"

- [ ] **Interactive Scenarios**
  - Quiz: "Is this alert real or false?" (10 scenarios, each with explanation)
  - Decision tree: Visual flowchart to diagnose anomaly type
  - Practice scenarios: Operator marks 5 example alerts, gets feedback
  - Difficulty levels: Beginner (obvious cases) → Advanced (edge cases)

- [ ] **Community Forum**
  - Operators post questions ("What does NOx contamination look like?")
  - Support team + peer operators answer
  - Monthly "wins" thread (anomalies caught, problems prevented)
  - Knowledge base: pinned solutions to common issues

- [ ] **Email Drip Campaign** (4+ emails)
  - Week 1: "Welcome to community of greenhouse experts"
  - Week 2: "5 common marking mistakes (and how to avoid them)"
  - Week 3: "Success story from a peer" (real operator example)
  - Week 4+: Weekly tips ("Spot NOx in 3 signs", "Humidity patterns you need to know")
  - Monthly: "Your community marked 500 alerts this month"

### Week 2-4: Multi-Vendor SCADA Integration
- [ ] **Priva API Driver**
  - OAuth authentication
  - Pull sensor data (real-time or 15-min intervals)
  - Map Priva schema → canonical schema
  - Error handling (API downtime, auth refresh)
  - Documentation + test suite

- [ ] **Hoogendoorn API Driver**
  - Similar to Priva driver
  - Handle any schema differences
  - Test with mock data (if real account unavailable)

- [ ] **Generic MQTT/Modbus Fallback**
  - For any vendor not explicitly supported
  - Operator configures topic mapping
  - Validates data before ingestion

### Week 4-6: LLM Reasoning Agent
- [ ] **Incident Database (pgvector)**
  - Store: anomaly events + operator feedback + outcome + root cause
  - Vector embeddings: convert incident descriptions to embeddings
  - Index: vector search to find similar past incidents
  - Schema: incident_id, timestamp, sensors, anomaly_type, operator_feedback, root_cause, resolution_time

- [ ] **LLM Agent (Claude API)**
  - Input: Current anomaly + sensor data + historical incidents
  - Process: 
    1. Search incident database (vector search: find similar past cases)
    2. Retrieve: top 3 similar incidents + their resolutions
    3. Reason: Claude generates hypothesis ("Looks like NOx contamination based on...")
    4. Confidence: 0–100% (based on similarity to past cases)
    5. Recommendation: Suggested action ("Check air intake seal")
  - Output: JSON {anomaly_type, confidence, recommendation, similar_incidents}

- [ ] **Feedback Loop for LLM**
  - Operator marks alert real/false + selects root cause from dropdown
  - System stores: alert, feedback, actual root cause (once operator confirms field)
  - Weekly: retrain vector embeddings with new incidents
  - Track: LLM confidence accuracy (week 1: 70%, week 8: 85% target)

### Week 5-6: React Dashboard Rebuild
- [ ] **Multi-Site Dashboard**
  - Home page: 10+ greenhouses, status overview (green/yellow/red)
  - Per-site drill-down: alerts, feedback trends, model performance
  - Per-alert detail: anomaly score, LLM reasoning, recommended action
  - Operator feedback widget: "Real / False Positive / Investigating"
  - Charts: anomaly trends, false positive rate, feedback latency

- [ ] **Export & Reporting**
  - Weekly summary PDF (email)
  - CSV export (anomalies + feedback + root causes)
  - Custom report builder (customer dashboard)

### Week 6+: Onboard 5+ Customers
- [ ] **Customer Setup Process** (per customer, 4-6 hours)
  - Provision: PostgreSQL schema, model registry entries, API keys
  - Connect: to their SCADA system (Priva, Hoogendoorn, or CSV upload)
  - Seed: historical data (2-4 weeks for baseline)
  - Fine-tune: LSTM-VAE transfer learning (30-60 min)
  - Deploy: customer-specific model + start daily inference
  - Notify: send onboarding email + training video link

- [ ] **Training Rollout** (per customer, coordinated)
  - Week 1: All operators get 5-min video + decision guide + forum access
  - Week 2: Portal goes live, operators start self-service
  - Week 3+: Identify Tier 2 candidates (engaged operators)
  - Week 4+: Offer Tier 2 optional calls to engaged

## Operator Training Plan (Phase 2)

### Tier 1: All Operators (Automated, Low-Touch)
```
Timing: Week 1 of onboarding

Deliverables:
  ├─ 5-min video (embedded in portal)
  ├─ One-page decision guide (PDF + postal option)
  ├─ Portal access + forum invite
  ├─ Email week 1: Welcome
  ├─ Email week 2: Mistakes to avoid
  ├─ Email week 3: Peer success story
  └─ Email week 4+: Weekly tips

Success Metric: 80%+ of operators watch 5-min video
```

### Tier 2: Optional Advanced Training (Medium-Touch, Selective)
```
Timing: Week 3+ (offered to engaged operators)
Eligibility: Marked 30+ alerts in first 2 weeks with 70%+ accuracy

What They Get:
  ├─ 30-min live video call (optional)
  ├─ Show their greenhouse's anomaly examples
  ├─ Practice marking real examples from their site
  ├─ "Advanced Feedback Curator" certificate (badge in portal)
  ├─ Monthly webinar (60 min, all Tier 2 + 3 operators)
  ├─ Priority support (dedicated Slack channel)
  └─ Early access to new features

Operator Cost: Free (company investment in retention)
Company Cost: ~€500 per operator (1 hour × €60/hour + portal infrastructure)

Success Metric: 30-40% of operators take Tier 2
```

### Tier 3: Premium Training (High-Touch, Limited)
```
Timing: Week 4+ (offered to high-value customers only)
Eligibility: Customer paying €5k+/year or managing 3+ sites

What They Get:
  ├─ 2-hour training session (live + recorded)
  ├─ Certification (formal training + exam, optional)
  ├─ Custom scenarios for their crop type (e.g., tomatoes vs. peppers)
  ├─ Quarterly check-in calls (15 min, strategic review)
  ├─ Dedicated support contact (Slack + email priority)
  ├─ Early access to new features
  └─ Access to LLM confidence metrics dashboard

Operator Cost: €2k–5k/year (premium tier)
Company Cost: ~€1.5k per operator (2 hours + ongoing)

Success Metric: 10-15% of customers take Tier 3 (high-value segment)
```

### Tier 4: Community & Self-Service (Continuous)
```
Ongoing:
  ├─ Forum discussions (peer-to-peer learning)
  ├─ Monthly community webinar (all operators invited)
  ├─ Email tips (weekly, increasingly specialized)
  ├─ Case study library (real scenarios from other greenhouses)
  └─ Seasonal guides ("Spring startup best practices", etc.)

Success Metric: Forum has 50+ discussions, high engagement
```

### Training Roll-Out Timeline
```
Week 1-2: Deploy portal + Tier 1 to all customers
  └─ All operators: 5-min video + decision guide + forum access

Week 2-4: Portal stabilization
  └─ Operators watch videos on their own time
  └─ Forum discussions begin
  └─ Monitor engagement metrics

Week 3+: Offer Tier 2 to engaged operators
  └─ Identify candidates (marked 30+ alerts)
  └─ Send personal invitation email
  └─ Schedule calls over next 2-4 weeks

Week 4+: Offer Tier 3 to premium customers
  └─ Customer success manager reaches out
  └─ Schedule 2-hour session + quarterly check-ins
```

## Phase 2 Success Metrics
- ✅ 5+ customers deployed with custom models
- ✅ Multi-vendor support working (Priva + Hoogendoorn + CSV fallback)
- ✅ 60%+ portal engagement (operators watch at least 1 video)
- ✅ 30-40% of operators take Tier 2 optional training
- ✅ 10-15% of customers take Tier 3 premium training
- ✅ Forum has 50+ active discussions
- ✅ LLM confidence improves: week 1 (70%) → week 8 (85%)
- ✅ Operator marking accuracy remains 70%+ across all customers
- ✅ Portal uptime 99.5%+

## Phase 2 Deliverables Summary
```
Engineering:
  ├─ Multi-vendor SCADA drivers (Priva, Hoogendoorn)
  ├─ React dashboard (multi-site, reporting)
  ├─ Incident database (pgvector)
  ├─ LLM reasoning agent (Claude API)
  ├─ Portal (videos, scenarios, forum)
  ├─ Email automation (drip campaigns)
  ├─ 5+ deployed customers
  └─ Monitoring + analytics dashboard

Training & Operations:
  ├─ 8-10 educational videos
  ├─ 10+ interactive scenarios
  ├─ Community forum (moderated)
  ├─ Email drip campaign (5+ emails)
  ├─ Tier 2 training call template
  ├─ Tier 3 training curriculum
  └─ Monthly webinar template

Documentation:
  ├─ Portal user guide
  ├─ SCADA driver setup guides (per vendor)
  ├─ LLM reasoning guide (how it works)
  ├─ Customer success playbook (onboarding)
  ├─ Training effectiveness metrics (tracking)
  └─ Forum moderation guidelines
```

## Phase 2 Go/No-Go Criteria (End of Month 6)
- ✅ **GO:** 5+ customers, 60%+ portal engagement, LLM 80%+ confidence, ready for Phase 3
- ⚠️ **HOLD:** 3-4 customers, 40% portal engagement, extend 4 more weeks
- ❌ **STOP:** <3 customers, <30% engagement, fundamental product-market fit issue

---

# PHASE 3: Self-Learning LLM + Decision Support (3-4 Months)

## Strategic Goals
- **Primary:** LLM learns from outcomes (what happened after each recommendation)
- **Secondary:** Recommendations become intelligent + contextual
- **Outcome:** AI becomes trusted advisor (75%+ operator trust in recommendations)

## Team Composition
- 1 ML engineer (outcome tracking, LLM fine-tuning)
- 1 backend engineer (decision support infrastructure)
- 1 frontend engineer (recommendation UI)
- 1 customer success manager (customer feedback)
- 1 content creator (case studies, webinars)

**Total person-months:** 15–18

## Key Differences from Phase 2
- **Learning loop closes:** System tracks what operator did after recommendation
- **Outcome database:** Records success/failure of each recommended action
- **LLM context expands:** Can reference past successful recommendations in similar situations
- **Personalization:** Recommendations adapt to each operator's style ("conservative" vs. "aggressive")

## Technical Focus Areas

### Outcome Tracking
```
Phase 2: "Looks like NOx. Try checking air seal." (no follow-up)
Phase 3: "Looks like NOx. Try checking air seal." + 
         Operator marks: "Confirmed NOx contamination"
         Action: "Operator cleaned air seal"
         Result: "Anomaly resolved in 2h, no crop damage"
         → System records: Recommendation valid + effective
```

### LLM Fine-Tuning
- Claude API calls → examples of good recommendations + outcomes
- Month 2: Retrain context window with successful past cases
- Month 3: Custom embeddings for operator personality + greenhouse context
- Month 4: Multi-step reasoning ("If NOx AND low CO₂, suspect both seal AND compressor")

### Recommendation Dashboard
- **Stage 1:** "System suggests checking X (based on 3 similar past cases)"
- **Stage 2:** "You successfully detected this type of issue 5 times. Confidence 88%"
- **Stage 3:** "Recommended action: Clean air seal (successful in 92% of similar cases in your region)"
- **Stage 4:** "Quick action approval: Reduce ventilation to save energy? (you've approved similar 15 times)"

## Training Plan (Phase 3)
Same as Phase 2, with enhancements:
- **New videos:** "How the system learns from your actions"
- **New case studies:** "From incident to insight: how feedback improved our greenhouse"
- **Tier 2 focus:** Operators become trainers (peer education)
- **Tier 3 expansion:** Outcome tracking + trust metrics

## Success Metrics (Phase 3)
- ✅ 10+ customers deployed (scaling from Phase 2)
- ✅ 75%+ operator trust in recommendations (survey)
- ✅ 70%+ of recommendations followed by operators
- ✅ 80%+ of followed recommendations successful (no adverse outcomes)
- ✅ LLM confidence 85%+ (consistently accurate)
- ✅ Portal engagement 70%+

---

# PHASE 4: Autonomous Control (3-4 Months)

## Strategic Goals
- **Primary:** Operator-approved emergency automation (e.g., reduce ventilation to save CO₂ on power failure)
- **Secondary:** Decision support escalates to suggested actions
- **Outcome:** Operators can delegate simple decisions to AI

## Key Difference
- **Manual → Semi-autonomous:** System can execute pre-approved actions without operator in loop
- **Emergency protocols:** "If power fails AND no manual override, reduce ventilation for 4h"
- **Approval chain:** High-confidence recommendations → operator approves once → system executes future

## Training Plan (Phase 4)
**New focus:** Tier 3 operators + autonomous action approval
- Training: "How to set up emergency protocols"
- Videos: "What autonomous actions can the system take?"
- Case studies: "Autonomous action prevented €10k crop loss"
- Quarterly review: "Your approved actions in Q1: 12 executions, 100% successful"

## Success Metrics (Phase 4)
- ✅ 10+ customers using autonomous control
- ✅ 90%+ autonomous action success rate (no unintended consequences)
- ✅ Operator satisfaction with automation 4.5+/5.0
- ✅ Measurable crop/energy improvements (€500+/year per greenhouse)

---

# Pricing Decision Framework

**Status:** OPEN — Finalize at start of Phase 2

### Options to Evaluate
1. **Hybrid License** (perpetual + annual support)
   - €2k perpetual license + €500/year support
   - Pros: European preference, predictable revenue
   - Cons: Lower LTV, complex billing

2. **Pure SaaS** (monthly subscription)
   - €150/month per greenhouse
   - Pros: Recurring revenue, scales with usage
   - Cons: May face resistance in Europe

3. **Value-Based** (% of savings)
   - Charge 10-20% of crop/energy savings generated
   - Pros: Aligned incentives
   - Cons: Complex ROI tracking, trust-dependent

4. **Freemium** (free + premium)
   - Free: 1 greenhouse + basic alerts
   - Premium: €500/year per additional greenhouse
   - Pros: Low-friction acquisition
   - Cons: Cannibalization risk

5. **Tiered SaaS** (small/medium/large)
   - Small: €100/month (1-5 greenhouses)
   - Medium: €250/month (5-20 greenhouses)
   - Large: €500/month (20+ greenhouses)
   - Pros: Captures value across segments
   - Cons: Complex packaging

### Decision Criteria
- **Revenue per customer:** €500+ annually (sustainable)
- **Gross margin:** 70%+ (software economics)
- **Churn rate:** <5% annually (sticky product)
- **NPS:** 50+ (strong recommendation)

### Timeline
- **Collect data:** Phase 1-2 customer feedback + willingness-to-pay surveys
- **Decide:** Start of Phase 2 (4-6 weeks into Phase 1.5)
- **Implement:** Week 1 of Phase 2 (before launching 5+ customers)

---

# Summary: Implementation Roadmap

| Phase | Timeline | Goal | Team | Customers | Pricing |
|-------|----------|------|------|-----------|---------|
| **1 (MVP)** | 3 mo | Proof of concept | 2-3 | 1 | Unfunded |
| **1.5** | 4-6 wks | Model adaptation | 1-2 | 2-5 | Unfunded |
| **2** | 4-6 mo | Multi-vendor scale | 4-5 | 5+ | Finalize pricing |
| **3** | 3-4 mo | Self-learning LLM | 4-5 | 10+ | Revenue model live |
| **4** | 3-4 mo | Autonomous control | 4-5 | 10+ | Premium tier focus |

**Total timeline:** 12-15 months from MVP to autonomous control  
**Total investment:** €200k–400k (team + infrastructure + training content)  
**Target ARR by Phase 4:** €150k–500k (10+ customers × €15k–50k/year)

---

# Next Steps
1. **Confirm Phase 1 timeline** (can you dedicate 3 months, 2-3 people?)
2. **Secure beta customer** (ideally with Priva system for proof)
3. **Week 1:** Technical setup (PostgreSQL, MQTT, Streamlit)
4. **Week 2:** Model training (LSTM-VAE on synthetic data)
5. **Week 3-8:** Operator feedback loop + weekly iteration
6. **Week 8-9:** Phase 1.5 planning (concurrent with Phase 1 end)

**Status:** Ready for sprint planning  
**Owner:** [Your name]  
**Date created:** May 12, 2026

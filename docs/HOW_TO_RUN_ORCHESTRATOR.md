# How To Run the Orchestrator

> **TL;DR** — The orchestrator is a Python script that uses the Google Antigravity SDK to plan and execute multi-agent tasks. You run it from the terminal inside the `ai-agronomist` project.

---

## Prerequisites

| Requirement | How to verify |
|---|---|
| Python 3.11+ venv | `venv/bin/python --version` |
| `google-antigravity` installed | `venv/bin/pip show google-antigravity` |
| `GEMINI_API_KEY` set | `echo $GEMINI_API_KEY` or check `.env` |

If the key is in `.env`, it is loaded automatically by `python-dotenv`.

---

## 1. Quick Start (Inline Task)

Pass a task directly with `-t`:

```bash
venv/bin/python src/orchestrator.py \
  -t "Review the LSTM-VAE architecture and file ADRs for Week 1 decisions"
```

### What Happens

1. **Planning** — The orchestrator agent (Gemini 2.5 Pro) decomposes your task into a phased execution plan.
2. **Approval** — The plan is printed to your terminal. You are prompted:
   ```
   Proceed with execution? (y/n):
   ```
   Type `y` to approve or `n` to cancel.
3. **Execution** — Subagents run (in parallel when possible), write output files, and report results.
4. **Memory Update** — `AGENT_MEMORY.md` is updated automatically.

---

## 2. Task File Mode

For complex or multi-paragraph prompts, write the task to a file and pass it with `-f`:

```bash
# Write the task to a file
cat > tasks/task5_architecture_review.txt << 'EOF'
Review the Week 1 foundation architecture (LSTM-VAE, training pipeline, tests, Docker)
and file Architecture Decision Records (ADRs) documenting major decisions.

## Components to Review
1. LSTM-VAE Model (src/models/lstm_vae.py) - Encoder-decoder, 8D latent space, VAE loss
2. Synthetic Data Generator (src/models/data_generator.py) - TimeGAN-inspired, 1000 sequences
3. Training Pipeline (src/models/training.py) - VAETrainer, TensorBoard, checkpoints
4. Unit Tests (tests/) - 116 tests, >92% coverage
5. Docker Environment (docker-compose.yml) - PostgreSQL, Jupyter, TensorBoard

## Deliverables
1. Architecture assessment → docs/review/ARCHITECTURE_ASSESSMENT.md
2. ADR-001: LSTM-VAE Architecture → docs/team/ADRs/ADR-001-lstm-vae-architecture.md
3. ADR-002: Synthetic Data Generation → docs/team/ADRs/ADR-002-synthetic-data-generation.md
4. ADR-003: Modular Architecture → docs/team/ADRs/ADR-003-modular-phase-1-5-architecture.md
5. Code quality review → docs/review/code_quality_review.md
6. Phase 2 recommendations → docs/review/phase2_recommendations.md
EOF

# Run it
venv/bin/python src/orchestrator.py -f tasks/task5_architecture_review.txt
```

---

## 3. Verbose Mode (Live Token Streaming)

Add `-v` to stream agent responses in real-time:

```bash
venv/bin/python src/orchestrator.py \
  -t "Review the LSTM-VAE architecture" \
  -v
```

This prints each token as it arrives from the subagents.

---

## 4. How the Orchestrator Communicates with You

### Decision Point: Execution Plan Approval

The orchestrator **always** pauses after planning and asks for your approval before executing anything:

```
📋 Execution Plan:

  Phase 1 (parallel):
    • architect → docs/review/ARCHITECTURE_ASSESSMENT.md
    • code-reviewer → docs/review/code_quality_review.md

  Phase 2 (sequential):
    • architect → docs/team/ADRs/ADR-001-lstm-vae-architecture.md
    • architect → docs/team/ADRs/ADR-002-synthetic-data-generation.md
    • architect → docs/team/ADRs/ADR-003-modular-phase-1-5-architecture.md

  Phase 3 (parallel):
    • code-reviewer → docs/review/phase2_recommendations.md [uses: architect]

Proceed with execution? (y/n):
```

- **`y`** → Agents start working.
- **`n`** → The run is cancelled. You can refine your task and re-run.

### During Execution

Agents log their progress to the terminal:
```
⚡ Executing Phase 1...
INFO - Starting [architect] -> docs/review/ARCHITECTURE_ASSESSMENT.md
INFO - Starting [code-reviewer] -> docs/review/code_quality_review.md
INFO - [architect] finished in 45s (success=True)
INFO - [code-reviewer] finished in 38s (success=True)
```

### After Execution

The orchestrator:
1. Prints `🎉 Orchestration complete!`
2. Auto-updates `AGENT_MEMORY.md` with what was built
3. All output files are written to the paths shown in the plan

---

## 5. Running Task 5: Architecture Review

Copy-paste this exact command to run the Week 1 Task 5 architecture review:

```bash
venv/bin/python src/orchestrator.py -v -f "implementation plan/WEEK_1_TASK_5_ARCHITECTURE_REVIEW.md"
```

Or with an inline prompt:

```bash
venv/bin/python src/orchestrator.py -v -t '
Review the Week 1 foundation architecture and file ADRs.

Components: src/models/lstm_vae.py, src/models/data_generator.py,
src/models/training.py, tests/, docker-compose.yml

Deliverables:
1. docs/review/ARCHITECTURE_ASSESSMENT.md - Full architecture review
2. docs/team/ADRs/ADR-001-lstm-vae-architecture.md
3. docs/team/ADRs/ADR-002-synthetic-data-generation.md
4. docs/team/ADRs/ADR-003-modular-phase-1-5-architecture.md
5. docs/review/code_quality_review.md
6. docs/review/phase2_recommendations.md
'
```

---

## 6. Available Agents

The orchestrator can assign tasks to these 7 agents:

| Agent | Model | Role |
|---|---|---|
| `architect` | gemini-2.5-pro | System design, ADRs, architecture decisions |
| `ml-engineer` | gemini-2.5-pro | ML model code, training pipelines |
| `backend-engineer` | gemini-2.5-flash | API endpoints, data layer, integrations |
| `code-reviewer` | gemini-2.5-pro | Code quality, review, compliance |
| `devops-lead` | gemini-2.5-pro | Docker, CI/CD, infrastructure |
| `qa-engineer` | gemini-2.5-flash | Tests, coverage, test strategy |
| `tech-lead` | gemini-2.5-pro | Cross-cutting concerns, tech debt, coordination |

---

## 7. Automated Loops (Post-Orchestration)

After the orchestrator finishes, run these loops for housekeeping:

```bash
# Generate commit message + PR description
venv/bin/python scripts/agent_loops.py --git

# Update CHANGELOG.md
venv/bin/python scripts/agent_loops.py --changelog

# Audit ADR compliance
venv/bin/python scripts/agent_loops.py --adr

# Validate guardrails
venv/bin/python scripts/agent_loops.py --validate-guardrails

# Run all loops at once
venv/bin/python scripts/agent_loops.py --all
```

---

## 8. Troubleshooting

| Problem | Solution |
|---|---|
| `GEMINI_API_KEY not set` | Add it to `.env` or `export GEMINI_API_KEY=...` |
| `ModuleNotFoundError: google.antigravity` | Run `venv/bin/pip install google-antigravity` |
| `Planning attempt failed` | Usually a rate limit — the orchestrator retries 3× with exponential backoff (65s → 130s → 260s) |
| Agent writes code instead of content | The orchestrator prompt already instructs agents to use file tools. Re-run with a clearer task. |
| Output file not created | Check logs — the agent may have written to a different path. Use `-v` to see full output. |

---

## 9. Architecture Overview

```
┌──────────────────────────────────────────────┐
│                   YOU (terminal)              │
│   venv/bin/python src/orchestrator.py -t ...  │
└───────────────┬──────────────────────────────┘
                │  task prompt
                ▼
┌──────────────────────────────────────────────┐
│         Orchestrator (gemini-2.5-pro)        │
│  Decomposes task → phased ExecutionPlan      │
│  ──────────────────────────────────────      │
│  Prints plan → asks: "Proceed? (y/n)"       │  ← YOU APPROVE HERE
└───────┬───────┬───────┬──────────────────────┘
        │       │       │  parallel dispatch
        ▼       ▼       ▼
   ┌────────┐ ┌────────┐ ┌──────────────┐
   │architect│ │ml-eng  │ │code-reviewer │
   │  Pro   │ │  Pro   │ │    Pro       │
   └───┬────┘ └───┬────┘ └──────┬───────┘
       │          │              │
       ▼          ▼              ▼
   output files written to docs/, src/, etc.
                │
                ▼
   ┌──────────────────────────────────┐
   │  Memory Agent (auto-update)     │
   │  → AGENT_MEMORY.md             │
   └──────────────────────────────────┘
```

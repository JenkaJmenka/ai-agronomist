# Code Refactoring: Repo-Wide Analysis

**Status:** Analysis-only — produces a plan document, does not execute changes
**Depends on:** None — should run FIRST among all five plans (see `00_SEQUENCING_OVERVIEW.md`), since its output may change how Plans 01-03 are implemented.
**Scope:** Whole repo at `~/Projects/ai-agronomist/`, with Claude Code determining specific focus areas rather than being told where to look in advance.

---

## Problem statement

The codebase has grown across multiple implementation phases (model code, data pipeline, Streamlit dashboard, orchestrator/agent scripts, tests) without a dedicated refactoring pass. Known friction points already on record: the dashboard has unclear caching boundaries (being addressed in Plan 01), and there was a previously resolved but indicative issue where the orchestrator's model routing config was tangled across multiple settings files (`~/.claude/settings.json` vs `.claude/settings.local.json`). These are signals, not the full picture — a structured analysis pass should surface what else exists.

## What this plan deliberately does NOT do

This is analysis and plan production only. Claude Code should not refactor anything in this pass — it should read the code, identify issues, and write a prioritized refactoring plan document. Execution of that plan is a separate, later task (explicitly not bundled in here, so the plan can be reviewed before any code changes happen).

## Areas the analysis should cover (non-exhaustive — let Claude Code expand)

- **Code organization**: module boundaries, whether data pipeline / model / dashboard / orchestrator code are cleanly separated or tangled
- **Duplication**: repeated logic across the model training script, the dashboard, and any orchestrator-side code (e.g., data loading or preprocessing implemented more than once)
- **Configuration sprawl**: settings files, environment variables, hardcoded paths/values that should be centralized (the prior settings.json/settings.local.json routing bug is a known example of this category)
- **Test coverage gaps**: per project history, Phase 3 QA (90%+ coverage target) was attempted but blocked by an Ollama timeout issue and remains unresolved — the analysis should report current actual coverage and identify what's untested, separate from the orchestration issue that blocked the prior attempt
- **Error handling consistency**: are failures handled consistently across modules, or ad hoc per file
- **Naming and structure conventions**: consistency of naming, file organization, whether it would read clearly to a new contributor
- **Dead code**: anything left over from earlier phases that's no longer used (e.g., abandoned experiments, superseded scripts)
- **Dependency hygiene**: unused imports/packages, version pinning issues, anything that would make environment setup fragile

## Deliverables

- [ ] A refactoring plan document (markdown), written by Claude Code, covering: findings by category, severity/impact ranking, and a proposed sequencing of refactor work
- [ ] Each finding should include file/line references — vague findings ("the code could be cleaner") are not useful; specific ones ("X is implemented in both data_loader.py and dashboard.py with slightly different logic, risk of drift") are
- [ ] An explicit call-out of anything that overlaps with Plans 01-03 (caching, UI robustness, anomaly detection), so those plans can be informed by it before they start
- [ ] No code changes in this pass — confirm the deliverable is a document, not a diff

## Success criteria

- The refactoring plan is specific enough that a future session could pick any single item from it and start work without needing to re-investigate from scratch
- The plan explicitly flags anything that should change how Plans 01-03 are implemented (e.g., "don't build the cache layer around `data_loader.py` as it stands — it's recommended for consolidation with X")

---

## Orchestrator prompt for Claude Code

```
You are performing a repo-wide code analysis for the AI Agronomist project at
~/Projects/ai-agronomist/. Read CLAUDE.md first for project rules and agent
delegation patterns.

OBJECTIVE: Produce a written refactoring plan. Do NOT modify, refactor, or fix
any code in this task — this is analysis and planning only. The output is a
markdown document, not a diff.

SCOPE: The whole repository. You decide what areas need the most attention
based on what you find — don't limit yourself to a pre-specified list. Cover
at minimum:
  - Code organization and module boundaries (is data pipeline / model /
    dashboard / orchestrator code cleanly separated?)
  - Duplicated logic across files (e.g., if preprocessing or data-loading
    logic is implemented more than once with slight variations)
  - Configuration sprawl (multiple settings files, hardcoded values that
    should be centralized — note the project has a documented history of a
    routing bug caused by conflicting settings.json/settings.local.json
    entries; check whether similar sprawl exists elsewhere)
  - Test coverage: report actual current coverage (run the test suite with
    coverage if a runner exists) and identify what's untested, particularly
    relative to the documented but unresolved Phase 3 QA goal of 90%+ coverage
  - Error handling consistency across modules
  - Naming/structure conventions and overall readability for a new contributor
  - Dead code from earlier phases no longer in use
  - Dependency hygiene (unused imports, loose version pins, fragile setup)

METHOD:
  - Read the code directly; don't rely on README/docs claims about what the
    code does without verifying against the actual implementation.
  - For every finding, cite specific file paths and line numbers or ranges.
    Vague findings are not acceptable — "data_loader.py:45-60 duplicates the
    column-validation logic also present in dashboard.py:120-140" is the
    right level of specificity.
  - Rank findings by impact: what's actively causing bugs or slowdowns vs.
    what's a maintainability nit.
  - Explicitly check whether any findings overlap with three other planned
    workstreams: (1) a caching layer being added around anomaly detection
    results and filtered views, (2) UI robustness improvements to the
    dashboard, (3) changes to anomaly detection training/calibration. If your
    findings would change how any of those should be approached, call that
    out as a specific, separate section in your output — this is important,
    don't bury it in general findings.

OUTPUT:
  Produce a single markdown document, REFACTORING_PLAN.md, in the project
  root, structured as:
    1. Executive summary (top 5 issues by impact)
    2. Findings by category (the list above), each with file/line references
    3. Proposed sequencing (what order to tackle items in, and why — e.g.,
       config consolidation before module reorganization if the former
       affects how the latter is tested)
    4. Overlap flags for the three workstreams listed above
    5. Explicit non-findings — areas you checked and found acceptable, so
       it's clear they were reviewed rather than skipped

  Do not implement any of the recommendations. Stop after producing the
  document and report a summary to the user.
```

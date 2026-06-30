# Frontend Migration: NextJS Scoping Pass

**Status:** Scoping only — no migration commitment yet
**Depends on:** Plans 01-03 should be substantially underway or complete first (see `00_SEQUENCING_OVERVIEW.md`). The actual trigger for this work isn't a calendar date — it's whether Plan 02 (UI robustness) surfaces interaction patterns that are fundamentally awkward in Streamlit regardless of how well it's optimized.
**Why scoping-only:** "Not sure yet — needs scoping first" was the explicit answer when asked whether this is a migration of the existing Streamlit dashboard or a greenfield build. That question has to be answered before any implementation prompt would even be well-formed — a migration plan and a greenfield build plan share almost nothing in terms of tasks.

---

## What needs to be decided before this becomes an implementation plan

1. **Is this replacing the Streamlit dashboard, or running alongside it?**
   Per the project's own Phase 2 roadmap (documented in project knowledge), a React-based production dashboard replacing Streamlit was already anticipated at the Phase 2 multi-customer stage — this isn't a new idea, but the question is whether now is that point, or whether this is being pulled forward.

2. **What's the actual trigger?** Candidates:
   - Streamlit has hit a genuine technical ceiling (certain interactions are impossible or very awkward to build well in Streamlit specifically — e.g., complex multi-panel real-time layouts, fine-grained client-side state, custom widget behavior)
   - Performance, even after Plan 01's caching — Streamlit's rerun-the-whole-script execution model has inherent limits no caching layer fully removes
   - A specific upcoming requirement (e.g., the multi-site dashboard for 5+ customers mentioned in the Phase 2 docs) that's a poor fit for Streamlit's single-session model
   - Or simply: this is being scoped now so it's ready when Phase 2 arrives, not because something is broken today

   The right plan looks different depending on which of these is true. If it's "ready for later," this should stay a lightweight scoping doc. If it's "needed now," it becomes a real project with its own timeline.

3. **What's the backend boundary?** If NextJS becomes the frontend, what serves it — does FastAPI (already in the stack) become a proper API backend that both the new frontend and any remaining Streamlit tooling call into? Or does the model-serving/data layer need its own refactor first (which would tie this to Plan 04's findings)?

4. **What's the migration unit?** If migrating, is it page-by-page (run both in parallel during transition), or a clean cutover? Given this is a solo-founder project, a parallel/incremental approach is almost certainly lower-risk than a cutover, but worth confirming explicitly rather than assuming.

## What this plan's first deliverable actually is

Not code. A short scoping document answering the four questions above, informed by:
- Plan 02's findings (does UI robustness work reveal a Streamlit ceiling?)
- Plan 04's findings (does the refactoring analysis reveal backend/API boundaries that would make a frontend swap easier or harder than expected?)
- A concrete inventory of current dashboard features/pages, so "migrate the dashboard" has an actual checklist rather than being an abstract goal

## Deliverables (for this scoping pass specifically)

- [ ] Inventory of current dashboard pages/tabs/features (what exists today, concretely)
- [ ] A decision recommendation (not a unilateral decision) on migration vs. greenfield vs. defer, with reasoning tied to the trigger-candidates above
- [ ] If migration or greenfield is recommended: a rough feature-parity checklist and an estimate of effort, broken down by feature area
- [ ] If defer is recommended: what specific signal should trigger revisiting this (so it doesn't become a vague someday-item)
- [ ] Explicit note on backend/API boundary implications, cross-referenced with Plan 04's findings if available by the time this runs

## Success criteria

- A clear, justified recommendation exists (migrate now / greenfield now / defer) rather than this remaining an open question indefinitely
- If proceeding, there's a concrete checklist, not just "build it in NextJS"
- The decision is revisited if Plan 02 or Plan 04 surface new information that changes the calculus

---

## Orchestrator prompt for Claude Code

```
You are running a SCOPING pass — not an implementation — for a potential
frontend migration from Streamlit to NextJS, for the AI Agronomist project at
~/Projects/ai-agronomist/. Read CLAUDE.md first for project rules.

OBJECTIVE: Produce a scoping document that answers whether and how a NextJS
migration should happen — do NOT write any NextJS code or scaffold a new
project in this task. This is explicitly a decision-support deliverable, not
an implementation.

STEP 1 — INVENTORY THE CURRENT DASHBOARD:
  - List every page, tab, and distinct feature in the current Streamlit
    dashboard concretely (e.g., "Climate tab: shows temperature/humidity/CO2
    time series with a shared timeline slider across N charts" rather than
    "the climate stuff"). This becomes the feature-parity checklist if a
    migration is recommended later.

STEP 2 — IDENTIFY STREAMLIT-SPECIFIC LIMITATIONS, IF ANY:
  - Based on what the dashboard actually does today (not hypothetically),
    identify any interaction patterns that are genuinely difficult or
    impossible to build well in Streamlit, as opposed to things that are
    merely currently unoptimized (which a caching/robustness pass, handled
    separately, would address instead). Be honest if you don't find any
    genuine ceiling — "nothing currently requires leaving Streamlit" is a
    valid and useful conclusion, don't manufacture a justification.

STEP 3 — CHECK THE BACKEND BOUNDARY:
  - Determine how cleanly the current data/model-serving logic is separated
    from the Streamlit presentation layer. If a FastAPI layer already exists
    or could easily be extracted, note that — it affects how hard a frontend
    swap would actually be. If logic is tangled directly into dashboard.py
    with no API boundary, note that too, as it changes the estimate
    significantly.
  - If a REFACTORING_PLAN.md exists from a prior analysis task, read it and
    incorporate any relevant findings about backend/API structure.

STEP 4 — CHECK PROJECT DOCS FOR EXISTING PHASE 2 PLANS:
  - The project's own roadmap documents reference a React-based production
    dashboard at the Phase 2 multi-customer stage. Check whether current
    plans/docs in the repo or project knowledge give more detail on what was
    originally envisioned, and note whether NextJS now vs. that original
    plan are aligned or in tension.

STEP 5 — PRODUCE THE SCOPING DOCUMENT:
  Write NEXTJS_SCOPING.md covering:
    1. Current dashboard feature inventory (from Step 1)
    2. Honest assessment of whether a genuine Streamlit ceiling exists (Step 2)
    3. Backend/API boundary assessment (Step 3)
    4. Alignment with prior Phase 2 plans (Step 4)
    5. A recommendation: migrate now / build greenfield now / defer — with
       reasoning, not just a conclusion
    6. If migrate or greenfield is recommended: rough effort estimate by
       feature area, and a suggested approach (parallel/incremental vs.
       cutover)
    7. If defer is recommended: the specific signal that should trigger
       revisiting this decision later

  Do not scaffold a NextJS project or write frontend code in this task. Stop
  after producing the document and report a summary to the user, including
  your recommendation stated plainly in the first paragraph of your summary
  so it doesn't get lost.
```

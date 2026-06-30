# Implementation Sequencing Overview

**Created:** June 30, 2026
**Purpose:** Coordinate execution order across the four implementation plans below. Each plan is standalone and has its own orchestrator prompt for Claude Code, but the order matters because later plans assume earlier ones are stable.

---

## The four plans

1. `01_PERFORMANCE_OPTIMIZATION_CACHING.md` — Two-layer caching for anomaly results + filtered views
2. `02_UI_ROBUSTNESS.md` — Robust UI interaction patterns (error handling, state management, upload flow)
3. `03_ANOMALY_DETECTION_COLD_START_IMPROVEMENT.md` — Fix obvious anomalies not being detected
4. `04_CODE_REFACTORING_ANALYSIS.md` — Claude Code repo-wide analysis + refactoring plan
5. `05_NEXTJS_MIGRATION_SCOPING.md` — Scoping pass before any migration decision is made

(Five files total — the NextJS item splits into a scoping pass first, since "needs scoping first" was the answer given.)

## Recommended order and why

### Step 1 — Code refactoring analysis (Plan 04) FIRST, but as analysis-only
Run Claude Code's repo-wide analysis before touching anything else. Reasoning: if the caching work and UI robustness work get built on top of code that's about to be restructured, you do the work twice. The analysis itself is read-only and low-risk — it produces a refactoring plan document, not code changes. Do **not** let Claude Code execute the refactor yet; just get the plan and read it.

### Step 2 — Performance optimization: caching (Plan 01)
Build the two-layer cache (disk cache for anomaly scores, in-memory cache for filtered views) on the current code structure. This is a contained, well-scoped change. If the Step 1 refactoring plan flags the data-loading or anomaly-scoring modules as needing a rewrite, fold that into how you implement the cache boundaries now rather than caching around code you're about to restructure.

### Step 3 — UI robustness (Plan 02)
Builds directly on Plan 01 — robust interaction (loading states, error boundaries, retry logic) needs the cache layer in place first, since a lot of "is this stuck or just slow" UX confusion goes away once cache hits are instant and cache misses show a clear loading state.

### Step 4 — Anomaly detection cold-start improvement (Plan 03)
Independent of 01/02 technically, but sequenced after them because: (a) you'll want the caching layer in place before iterating on model changes, so you're not re-running slow inference on every experiment, and (b) this is the highest-uncertainty, highest-effort item — better to have the UI stable before diagnosing model behavior through it.

### Step 5 — NextJS migration scoping (Plan 05)
Last, and deliberately scoping-only. Don't commit engineering time to a frontend rewrite until Plans 01–03 prove out the current architecture's ceiling. If Plan 02 makes Streamlit's limitations clear (e.g., certain interaction patterns are fundamentally awkward in Streamlit no matter how well-cached), that's the actual trigger for migration, not a calendar date.

## Cross-cutting dependency notes

- Plan 04's refactoring analysis should explicitly flag any code paths that Plans 01–03 are about to modify, so you know whether to refactor-then-build or build-then-refactor for each module.
- Plan 03 (cold start) may surface findings that change Plan 01's cache invalidation logic — e.g., if `seq_len` or thresholds become tunable parameters in the UI, the cache key needs to include them. Plan 01 is written with this in mind (see its "Future-proofing" section).
- None of these plans touch pricing, GTM, or training content — out of scope by design.

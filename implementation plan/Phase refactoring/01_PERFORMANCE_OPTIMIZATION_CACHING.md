# Performance Optimization: Anomaly Data Caching

**Status:** Ready for implementation
**Depends on:** Read Plan 04's refactoring analysis output first if available (see `00_SEQUENCING_OVERVIEW.md`)
**Explicitly out of scope:** Optimizing the anomaly detection algorithm itself (LSTM-VAE inference speed, Isolation Forest speed) and optimizing the CSV upload/ingestion path. This plan only addresses *not re-running already-completed work* when the UI re-renders.

---

## Problem statement

Filtering or switching views in the dashboard currently appears to re-trigger processing that has already happened once. The user should never pay the cost of re-computing anomaly scores just because they moved a slider or switched tabs. There are two genuinely different things being cached here, and conflating them is the likely root cause of the current slowness — they need different invalidation rules and different storage layers.

## The two-layer model

### Layer 1: Anomaly detection results cache (expensive, rare, persistent)

This is the LSTM-VAE reconstruction error / Isolation Forest anomaly score output for a given dataset run through a given model version. It does not depend on what the user is currently looking at in the UI — it's computed once per (dataset, model) pair and should never be recomputed just because someone changed a date filter.

- **Cache key:** `hash(dataset_file_contents) + model_version_id` — not filename, not filter params. If the same CSV is re-uploaded, it should hit cache even if renamed. If the model is retrained (new version in the model registry), it should miss and recompute.
- **Storage:** Disk-backed (e.g., a local SQLite table, or pickled/parquet files keyed by hash, depending on what's idiomatic in the existing codebase — Claude Code should check what's already used for the model registry and reuse that pattern rather than introducing a third storage mechanism).
- **Survives:** App restarts, container restarts. This is the expensive thing — losing it on every restart defeats the purpose.
- **Invalidation:** Only on dataset change or model version change. Never on UI filter change.

### Layer 2: Filtered/aggregated view cache (cheap-ish, frequent, in-memory)

This is the slice-and-resample step: taking the full anomaly results from Layer 1 and producing what the current tab/timeline-slider/chart actually needs to render (a date-bounded subset, a downsampled series for the chart, a tab-specific column selection).

- **Cache key:** `dataset_hash + model_version_id + filter_params` (date range, selected sensors, active tab, resample interval — whatever varies per-view).
- **Storage:** In-memory, via `@st.cache_data` if staying on Streamlit. Streamlit's own hashing of function arguments handles most of this automatically — the main fix is making sure the filtering/resampling function is actually wrapped in `@st.cache_data` and that its arguments are the filter params, not the entire dataframe re-read from disk.
- **Survives:** Session only. Fine to lose on restart — recomputing a filtered slice from already-cached Layer 1 results is cheap.
- **Invalidation:** Any filter change. This is supposed to invalidate often; that's fine because it's cheap.

## Why this two-layer split matters

If there's currently a single cache (or no cache) covering both, two failure modes are likely happening:
- Filter changes are triggering full anomaly re-inference (Layer 1 work happening on Layer 2 triggers) — this is the main suspected cause of "filtering re-processes everything."
- Or, the dataset is being re-read from disk on every interaction because `pd.read_csv()` isn't cached at all, separate from whether anomaly scores are cached.

Claude Code's diagnostic step (below) should confirm which of these is actually happening before writing the fix, rather than assuming.

## Future-proofing note for cache key design

If Plan 03 (cold-start anomaly improvement) introduces tunable parameters — e.g., adjustable reconstruction error threshold, adjustable `seq_len`, manual seasonal baseline override — those become part of the Layer 1 cache key, not Layer 2, since they change the anomaly scores themselves, not just the view. Design the cache key as an extensible dict/struct now so adding a parameter later doesn't require restructuring the cache.

## Deliverables

- [ ] Diagnostic pass: confirm what's actually being recomputed today and why (don't skip this — see prompt below)
- [ ] Layer 1: persistent disk cache for anomaly detection results, keyed by dataset hash + model version
- [ ] Layer 2: in-memory cache for filtered/aggregated views, keyed by dataset hash + model version + filter params
- [ ] Cache invalidation logic: dataset re-upload detection (hash-based, not filename-based), model version bump detection
- [ ] A way to manually bust the cache (for debugging) — e.g., a hidden button or CLI flag, not exposed to end operators
- [ ] Logging/metrics: cache hit vs. miss, so you can verify the fix actually worked rather than assuming
- [ ] Tests: cache returns identical results to uncached path; cache correctly invalidates on dataset change; cache correctly invalidates on model version change; cache correctly does NOT invalidate on filter-only change

## Success criteria

- Switching tabs or moving the timeline filter does not trigger LSTM-VAE or Isolation Forest inference (verify via logging/profiling, not just "feels faster")
- Re-uploading the identical CSV (even renamed) hits Layer 1 cache
- Retraining/promoting a new model version correctly invalidates Layer 1 cache for affected datasets
- Cache hit path is at least 10x faster than cache miss path for the same operation (rough target — refine once you have real numbers)

---

## Orchestrator prompt for Claude Code

```
You are implementing a two-layer caching system for the AI Agronomist Streamlit
dashboard at ~/Projects/ai-agronomist/. Read CLAUDE.md first for project rules —
in particular, the orchestrator (you) must never write code directly if there are
subordinate agents configured for this; check CLAUDE.md for the current agent
routing rules before proceeding, and follow whatever delegation pattern is defined
there.

OBJECTIVE: Eliminate redundant recomputation when the user interacts with the
dashboard UI (changes filters, switches tabs, moves the timeline slider). Do NOT
optimize the anomaly detection algorithms themselves (LSTM-VAE, Isolation Forest)
and do NOT optimize the CSV upload/ingestion path — those are explicitly out of
scope for this task.

STEP 1 — DIAGNOSE BEFORE FIXING (do not skip):
Find the actual dashboard source (likely dashboard.py or similar). Identify:
  - Where is the raw CSV loaded? Is it wrapped in @st.cache_data or equivalent?
  - Where does anomaly detection (LSTM-VAE inference, Isolation Forest scoring)
    get invoked? Is it re-invoked on every rerun, or only once per dataset?
  - Where do tabs (st.tabs) and the timeline slider live relative to the data
    loading and anomaly scoring calls? If scoring happens inside the same
    function scope as a widget that changes on every interaction, that's
    likely triggering recomputation.
  - Report back concretely: "X is recomputed every time Y changes, because Z."
    Do not proceed to Step 2 until you can state this clearly with file/line
    references.

STEP 2 — IMPLEMENT LAYER 1 (persistent disk cache for anomaly results):
  - Cache key = hash of the dataset file contents (not filename) + model
    version identifier (check the model registry mentioned in CLAUDE.md/project
    docs for how model versions are currently tracked; reuse that scheme).
  - Storage: check what's already used elsewhere in the project for
    persistence (SQLite? pickle? parquet?) and reuse that pattern rather than
    introducing a new dependency, unless nothing suitable exists — in that case
    propose one option and wait for confirmation before adding a new library.
  - This cache must survive app/container restarts.
  - Invalidate ONLY on dataset content change or model version change — never
    on UI filter/tab changes.

STEP 3 — IMPLEMENT LAYER 2 (in-memory cache for filtered views):
  - Cache key = dataset hash + model version + the specific filter parameters
    in play (date range, selected sensors, active tab, resample interval —
    enumerate the actual ones used in the dashboard).
  - Use @st.cache_data (or the project's existing equivalent) for this layer.
  - This layer is allowed to be lost on restart.

STEP 4 — INSTRUMENT:
  - Add lightweight logging or counters showing cache hit vs. miss for both
    layers, so the result can be verified rather than assumed. Don't build a
    full metrics dashboard — a log line or a small debug panel is enough.

STEP 5 — TEST:
  - Write tests verifying: (a) cached and uncached paths produce identical
    anomaly results, (b) re-uploading an identical CSV under a different
    filename hits Layer 1 cache, (c) a model version bump invalidates Layer 1
    cache, (d) changing a filter does NOT invalidate Layer 1 cache and does
    NOT trigger anomaly re-inference.

STEP 6 — REPORT:
  - Summarize what was changed, file by file. Include a before/after note on
    what specifically used to recompute on filter change and now doesn't.
  - Do not mark this complete until Step 5 tests pass.

Constraints:
  - Do not modify the anomaly detection model code itself (model architecture,
    training, inference logic) — caching wraps around it, it doesn't change it.
  - Do not change the CSV upload/parsing flow.
  - If you find the refactoring analysis from a prior Claude Code run (likely
    a file describing repo-wide refactor recommendations), check whether it
    flags the data-loading or scoring modules — if so, note any tension between
    "cache around existing structure" vs. "this module is about to be
    restructured" and ask before proceeding rather than guessing.
```

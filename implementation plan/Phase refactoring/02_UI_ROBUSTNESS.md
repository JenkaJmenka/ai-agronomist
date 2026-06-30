# UI Robustness Improvements

**Status:** Ready for implementation
**Depends on:** Plan 01 (caching) should land first — robust loading/error states are much easier to reason about once cache hit/miss is the only thing determining wait time.
**Explicitly out of scope:** Performance of the underlying anomaly detection or upload processing (that's Plan 01's job to make feel fast via caching — this plan is about *interaction quality*, not raw speed).

---

## Problem statement

The instruction was: "don't focus on performance optimization for anomaly detection processing or uploading the data — I need to provide more robust interaction with the system from the UI." This is about resilience and clarity of interaction, not speed: what happens when something is slow, fails, is ambiguous, or the user does something unexpected.

## Areas to cover

This list is a starting point — Claude Code should expand it after actually reading the dashboard code, since some of these may already be partially handled.

### 1. Loading and processing feedback
- Every action with a perceptible delay (cache miss on Layer 1 from Plan 01, model inference, file upload/parsing) needs a visible, specific loading state — not a generic spinner with no context. "Re-training model on uploaded data (est. 30–60s)..." is more useful than a bare spinner.
- Distinguish between "fast cache hit, nothing to show" and "actually working" — the user should never wonder if a click did anything.

### 2. Upload flow resilience
- What happens if the uploaded CSV is malformed, missing expected columns, has wrong units, or is a duplicate/re-export of an already-uploaded file (per the project's existing re-export detection logic)? Each of these needs a clear, specific message — not a stack trace or silent failure.
- What happens if upload is interrupted or the file is very large? Should there be a progress indicator, a size limit with a clear message, or chunked handling?
- If validation fails, the user should see exactly what's wrong (e.g., "Missing column: CO2_ppm" rather than a generic parse error).

### 3. Filter and state management
- Does changing a filter ever leave the UI in an inconsistent state (e.g., chart shows old data while a new filter is "loading," with no visual cue that they don't match)?
- Are filter selections preserved when switching tabs, or does the user lose their place?
- What happens if a filter combination produces zero results (e.g., date range with no data)? Should show an explicit empty state, not a blank chart.

### 4. Error boundaries
- If the LSTM-VAE or Isolation Forest inference throws (bad input shape, NaN propagation, etc.), does the whole dashboard crash, or does the error stay contained to the affected panel with a clear message?
- Network/connection issues (if any external calls exist) should degrade gracefully, not hang indefinitely.

### 5. Operator feedback interactions
- Per the project's operator feedback loop design (marking alerts real/false), confirm the marking action gives immediate visual confirmation and is not silently lost if the page reruns.

## Deliverables

- [ ] Audit pass: Claude Code reads the dashboard and lists every user-facing interaction point and its current failure/loading behavior (this becomes the actual task list — don't write it from assumptions)
- [ ] Loading states for all operations identified as slow (post-Plan-01, this should mainly be Layer 1 cache misses)
- [ ] Upload validation with specific, actionable error messages
- [ ] Empty-state handling for filters that produce no data
- [ ] Error containment so a failure in one panel doesn't crash the whole app
- [ ] Filter state persistence across tab switches (if not already present)
- [ ] Tests for the above failure modes (malformed CSV, empty filter result, etc.)

## Success criteria

- No user-facing action results in a silent failure, indefinite spinner, or stack trace
- Every error message tells the user what specifically went wrong and, where possible, what to do about it
- Switching tabs never loses the user's current filter selection

---

## Orchestrator prompt for Claude Code

```
You are improving UI robustness for the AI Agronomist Streamlit dashboard at
~/Projects/ai-agronomist/. Read CLAUDE.md first for project rules and agent
delegation patterns before proceeding.

OBJECTIVE: Improve the quality and resilience of user interaction with the
dashboard — loading feedback, error handling, upload validation, and state
consistency. This is explicitly NOT about making anomaly detection or upload
processing faster (that's handled separately) — it's about making the
interaction feel reliable and informative regardless of how long things take.

STEP 1 — AUDIT (do not skip):
Read through the dashboard source and enumerate every user-facing interaction
point: file upload, filters, tab switches, timeline slider, any alert-marking
/feedback UI, any buttons or forms. For each, identify current behavior when:
  (a) the operation is slow
  (b) the operation fails
  (c) the input is invalid or produces no results
Report this as a concrete list with file/line references before writing any
code. Some of these may already be handled reasonably — only flag genuine gaps.

STEP 2 — PRIORITIZE:
From the audit, propose a prioritized list of fixes. Flag anything that seems
like a significant rework (e.g., if state management has no consistent pattern
at all and needs a structural change) rather than just patching symptoms —
ask before doing large structural changes.

STEP 3 — IMPLEMENT (after confirmation of the prioritized list):
  - Add specific, contextual loading indicators for slow operations.
  - Add upload validation that reports specific problems (missing columns,
    bad types, empty file, suspected duplicate/re-export per the project's
    existing re-export detection logic if present) rather than generic errors.
  - Add empty-state UI for filter combinations that produce no data.
  - Wrap risky operations (model inference, file parsing) so failures are
    contained to the relevant panel/component rather than crashing the whole
    app — use try/except with user-facing messages, not bare exceptions.
  - Ensure filter selections persist across tab switches (check Streamlit
    session_state usage; if filters aren't currently stored in session_state,
    that's likely the root cause of any state loss).

STEP 4 — TEST:
  - Simulate a malformed CSV upload (missing column, wrong delimiter, empty
    file) and verify the error message is specific and the app doesn't crash.
  - Simulate a filter combination with no matching data and verify a clear
    empty state appears instead of a blank/broken chart.
  - Simulate an inference failure (e.g., mock a bad input shape) and verify
    it's contained to the relevant panel.

STEP 5 — REPORT:
  - List what was changed and why, referencing the audit findings from Step 1.
  - Flag anything from the audit that was deliberately NOT fixed in this pass
    and why (e.g., out of scope, needs a larger structural change).

Constraints:
  - Do not modify anomaly detection model logic.
  - Do not modify the caching logic from the separate caching task — if you
    need to add a loading state around a cached operation, hook into its
    existing hit/miss signal rather than re-implementing timing logic.
  - Keep changes additive and incremental — this is a robustness pass, not a
    rewrite. If you think a rewrite is warranted, say so and stop rather than
    doing it unprompted.
```

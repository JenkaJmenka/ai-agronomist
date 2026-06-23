# Code Review — Week 1 Phase 1.2: Pattern Detector (7 Types)

**Verdict: PASS WITH NOTES**

All STOP-WORK guardrails pass. The known single-sensor crash is judged **non-blocking** (loud failure on a configuration unreachable in production, tracked by a strict xfail — see "Known Issue Judgment"), but it is a real contract violation and must be fixed the next time these modules are touched; the one-line fix is verified below. The two-file split, config externalization (19 `PATTERN_*` constants), and the 8 documented spec deviations are all sound. Test quality is high: 84 seeded, independent, behavior-focused tests; module results independently re-run by this reviewer (**83 passed + 1 xfailed in 0.08s**, matching the orchestrator's gate report).

Files reviewed: `src/models/pattern_detector.py` (268 lines), `src/models/pattern_scores.py` (299 lines), `tests/models/test_pattern_detector.py` (789 lines), `src/config.py:258-328` (`PATTERN_*` block), against the Phase 1.2 spec (`implementation plan/Phase 1.9.1/WEEK_1_PHASE_1_2_PATTERN_DETECTOR.md`), `.claude/guardrails/*.md`, AGENT_MEMORY (Phase 1.2 ml-engineer + QA entries), and the Phase 1.1 review for severity calibration. `src/models/lstm_vae.py` is **not in scope**: the detector is pure numpy and never touches the torch model (imports verified: stdlib + numpy + `src.config` + `src.models.pattern_scores` only).

---

## Guardrail Checklist

### `.claude/guardrails/universal-guardrails.md`

| Guardrail | Result | Evidence |
|---|---|---|
| STOP: Tight coupling LSTM-VAE ↔ API layer | PASS | `pattern_detector.py:29-49` imports logging/typing/numpy/config/pattern_scores; `pattern_scores.py:12-36` imports dataclasses/typing/numpy/config. No `src.api`, no torch, no model reference anywhere. Grep-verified. |
| STOP: Secrets/API keys hardcoded | PASS | Grep for eval/subprocess/os.system/password/secret/token/api_key over all four files: zero hits. |
| STOP: Checkpoint saved without metadata | N/A | No checkpointing in scope (pure statistical detector; baseline is in-memory only). |
| STOP: DB schema without tenant_id | N/A | No database code in scope. |
| STOP: Test coverage <80% on critical paths | PASS | Both modules at 100% per QA (`docs/coverage/phase1.9.1-pattern-detector-coverage.md`); full gate 627 passed, TOTAL 94.31% (gate 90%). Module suite independently re-run by reviewer: 83 passed + 1 xfailed. |
| STOP: Major design change without ADR | PASS, with note | The 8 spec deviations are algorithm-level corrections of demonstrably buggy spec reference code, documented in module docstrings, AGENT_MEMORY, and pinned by tests. Not an architecture change; ADR not required. See "Spec Deviation Assessment". |
| WARN: Hard-coded thresholds | PASS, one exception | All tunables externalized to `src/config.py:258-328` (19 constants, each commented). One residual magic number: the lag search-window divisor `len(x) // 4` (`pattern_scores.py:276`) — see F2. |
| WARN: Missing error handling | PARTIAL | Input validation is thorough (rank, emptiness, length, sensor count, NaN/Inf at both `set_baseline` and `detect`; `pattern_detector.py:213-268`). The gap is the single-sensor constructor contract — see F1. |
| WARN: Code duplication (3+ blocks) | PASS (2 blocks noted) | Below the 3-block guardrail threshold; the z-score expression appears twice (`pattern_scores.py:112, 229`) — see F3. |
| WARN: Silent failures (bare except, pass) | PASS | No bare excepts. `_safe_corrcoef` (`pattern_scores.py:237-242`) suppresses numpy warnings deliberately and zeroes NaN entries — this is a *documented, tested* numerical-stability choice (constant-channel correlation is undefined; zeroing keeps downstream means finite), not a swallowed error. Tested at `test:259-264, 710-716`. |

### `.claude/guardrails/ml-guardrails.md`

| Guardrail | Result | Evidence |
|---|---|---|
| STOP: LSTM-VAE spec locked | PASS (N/A) | Model architecture untouched; detector never imports or calls the model. |
| STOP: Checkpoint without epoch/loss/accuracy/timestamp/model_config | N/A | No checkpointing. |
| STOP: NaN/Inf in model outputs with no detection | PASS | No model outputs in scope. For its own numerics: non-finite **inputs** are rejected at both boundaries (`pattern_detector.py:236-240, 263-267`; tested for NaN, +Inf, −Inf at baseline and detect time, `test:418-428, 442-446, 591-595`); internally-generated NaNs (corrcoef of constant channels, zero FFT power) are explicitly guarded (`pattern_scores.py:237-242, 210-214, 265`). No path emits NaN/Inf in scores — QA's edge tests assert finiteness. |
| STOP: Training loop with no early stopping | N/A | No training. |
| STOP: Hard-coded sensor ranges | PASS | Sensor names default to `config.SENSOR_NAMES` (`pattern_detector.py:107`); no physical ranges hardcoded — saturation bounds derive from the *baseline data* min/max, which is the correct design (per-deployment, not global). |
| WARN: beta/dropout/lr limits | N/A | No hyperparameters touched. |

### `.claude/guardrails/red-flags.md`

| Red flag | Result | Evidence |
|---|---|---|
| 1. Writing outside file ownership | PASS | ml-engineer: `src/models/` + `src/config.py`; qa-engineer: `tests/models/`; this review: `docs/review/`. All per the CLAUDE.md ownership table. |
| 2. `os.system` / `subprocess shell=True` / `eval()` | PASS | None (grep-verified). |
| 3. Hardcoded password/token/API key | PASS | None (grep-verified). |
| 4. DELETE/DROP without tenant_id | N/A | No queries. |
| 5. `model.save()` without metadata JSON | N/A | No saving. |
| 6. Test file modifies implementation files | PASS | Tests only import; the QA single-sensor test hand-builds a local `PatternBaseline` instance (`test:649-665`) rather than patching the module. |
| 7. Circular imports `src/models/` ↔ `src/api/` | PASS | One-directional: detector → scores → config. |

---

## Known Issue Judgment — single-sensor crash (QA xfail)

**Judgment: NON-BLOCKING → does not force NEEDS FIXES.** Logged as F1 (MODERATE) with a mandatory fix-on-next-touch.

**Independently reproduced by this reviewer:** `PatternDetector(sensor_names=["solo"])` + `set_baseline((20,20,1))` stores a **0-d** `correlations` array (`np.corrcoef` of a single variable is scalar; `pattern_scores.py:83, 241-242` — `nan_to_num` preserves the 0-d shape), and `detect()` then raises `ValueError: input array must be 2-d` from `np.triu_indices_from` (`pattern_scores.py:132`) before `score_dependency`'s own `upper_i.size == 0` guard (`:133`) can fire.

Why this does not block, against the calibration set by the Phase 1.1 review (where NEEDS FIXES was reserved for a *silent* NaN→plausible-output path violating an ML STOP-WORK guardrail):

1. **The failure is loud, not silent.** It raises immediately at the first `detect()` call — no corrupted scores, no NaN propagation, no plausible-but-wrong output ever reaches a consumer. No STOP-WORK condition is met.
2. **Unreachable in production.** `config.SENSOR_NAMES` has 4 sensors; every integration path uses the default constructor. Only a caller explicitly passing a 1-element `sensor_names` hits it.
3. **Tracked, not forgotten.** The strict xfail (`test:675-692`) documents the root cause and will flip to a hard suite failure (XPASS-on-strict) the moment the bug is fixed, forcing the test to be promoted — it cannot rot silently.
4. **The fix is verified-trivial.** This reviewer confirmed that making `correlations` 2-d makes `score_dependency` return `(0.0, [])` correctly through the existing guard. One line: `return np.atleast_2d(np.nan_to_num(correlations, nan=0.0))` in `_safe_corrcoef` (`pattern_scores.py:242`) fixes **both** producers (baseline at `:83` and detect-time at `:131`), makes the currently-dead `:133-134` guard reachable through the public API, and keeps drift/spike/saturation/cyclic — all well-defined for one sensor — working. This is preferable to rejecting `len(sensor_names) < 2` in the constructor, which would narrow the public contract unnecessarily.

What keeps it MODERATE rather than MINOR: the constructor *documents* that "Length defines the expected input_dim" and validates only emptiness (`pattern_detector.py:96-110`), so the class makes a contract it cannot honor, and the eventual error message (`input array must be 2-d`, a raw numpy internals message) gives the caller no clue. Fail-fast at the boundary is an explicit org rule.

**Required follow-up (next phase that touches these modules):** apply `np.atleast_2d` in `_safe_corrcoef`, promote the xfail to a passing test, and drop the hand-built-baseline workaround in `TestQaSingleSensor`.

---

## Spec Deviation Assessment

All 8 deviations recorded by the ml-engineer were checked against the spec's reference code; all are sound corrections, several fixing bugs that would have failed the spec's own tests:

1. **Lag rewrite** (`pattern_scores.py:274-299`) — Correct. The spec's negative-lag branch (`sequence[:-lag, i]` vs `sequence[-lag:, j]` with `lag<0`) slices mismatched lengths (e.g. 3 vs 17 at lag=−3) and `np.corrcoef` of mismatched arrays is garbage/error; and "any non-zero argmax ⇒ 1.0" fires on pure noise. The implementation's symmetric windowing (`:291-299`), baseline-correlation candidate gating, minimum peak correlation, and zero-shift improvement margin are each config-backed and the margin correctly defuses the dict-ordering tie quirk in `max()` (`:281` — a tie between lag 0 and a non-zero lag yields improvement 0 < margin ⇒ not lagged).
2. **Saturation redefinition** (`:172-186`) — Correct. The spec's `min==max` test is literally a constant-value test, indistinguishable from drift and blind to actual boundary-railing. The band-of-baseline-min/max definition separates "stuck at mean" (drift only, `test:325-330`) from "railed at boundary" (`test:312-323`).
3. **Cyclic rewrite** (`:189-219`) — Correct. The spec compares non-DC power against `power.max()` *including the dominating DC bin*, making nearly every signal score ~1.0. The baseline-vs-sequence dominant-bin power-fraction comparison is principled, and returning 0.0 on seq-length mismatch (`:199`) is right — FFT bins don't translate across lengths (see INFO-2 for the consumer-facing consequence).
4. **Spike rescoring** (`:104-122`) — Correct. The spec's outlier-count/total-points score caps a single-point spike at 0.0125, failing the spec's own `>0.3` assertion. Peak-z scaling with a max-duration gate (sustained shifts excluded, `test:209-214, 742-749`) matches the stated "sudden short deviation" semantics.
5. **Cascade redefinition** (`:222-234`) — Correct. The spec's mean |corr| of deviations scores high on *normal* correlated data and NaNs on constant shifts; "≥2 sensors simultaneously deviating at mean |z| > 2" is the actual stated semantics.
6. **Thresholds → config** (`config.py:258-328`) — Correct per the no-magic-numbers rule; the spec's constructor kwargs (`drift_variance_threshold`, `spike_z_score_threshold`) are replaced by 19 named, individually-commented constants.
7. **`_safe_corrcoef` NaN guards** (`:237-242`) — Correct and necessary (constant channels are a tested real input).
8. **Per-pattern affected sensors** (every scorer returns indices; `pattern_detector.py:166-169`) — Correct. The spec computed affected sensors only for drift/spike and returned *all* sensors otherwise, which would have made `affected_sensors` meaningless for 5 of 7 patterns.

Naming note: the spec's example output uses `"dependency_violation"` while its own `PATTERN_TYPES` says `"dependency"`; the implementation consistently uses `"dependency"`. Right call.

**Spec deliverable not delivered:** "Dashboard: Pattern visualization" — explicitly recorded as out of ml-engineer scope in AGENT_MEMORY; remains on the Phase 1.2 pending list. Not a review blocker.

---

## Findings (by severity)

### F1 — MODERATE: Single-sensor `detect()` crashes with a raw numpy error, violating the constructor's documented contract
`src/models/pattern_scores.py:83, 131-132, 241-242`; `src/models/pattern_detector.py:96-110`; tracked by `tests/models/test_pattern_detector.py:675-692` (strict xfail)

Full analysis and non-blocking judgment above. **Fix on next touch:** `np.atleast_2d` in `_safe_corrcoef` (verified by this reviewer to restore correct `(0.0, [])` dependency scoring), promote the xfail.

### F2 — MINOR (WARN-class): Hardcoded lag-window divisor
`src/models/pattern_scores.py:276`

`max_lag = min(PATTERN_LAG_MAX_STEPS, len(x) // 4)` — the `// 4` ("search at most a quarter of the sequence") is a tuning heuristic living outside `src/config.py`, the only one in the module. Extract to e.g. `PATTERN_LAG_MAX_SEQ_FRACTION` alongside its 11 sibling constants. One-line change + comment.

### F3 — MINOR: Duplicated z-score expression
`src/models/pattern_scores.py:112, 229`

`np.abs(sequence - baseline.mean) / (baseline.std + NUMERICAL_EPSILON)` appears verbatim in `score_spike` and `score_cascade`. Two occurrences — below the guardrail's 3-block WARN threshold, but the org rule says "code in 2+ places → extract". A `_z_scores(sequence, baseline)` helper would also guarantee spike and cascade can never drift apart on epsilon handling.

### F4 — MINOR: `detect_batch` has no batch-level validation
`src/models/pattern_detector.py:179-188`

A caller passing a 2-D `(seq_len, input_dim)` array (a single sequence, the most likely misuse) gets iteration over *rows*: each 1-D row fails `_validate_sequence` with `"Sequence must have shape (seq_len, 4), got (4,)"` — a true but misleading message that doesn't say "you forgot the batch axis". A cheap `ndim == 3` guard naming the expected `(batch, seq_len, input_dim)` shape would fail faster and clearer. (Empty batch → `[]` and per-sequence NaN propagation are both correct and tested, `test:629-639`.)

### F5 — MINOR: `PATTERN_MIN_SEQ_LEN = 2` admits statistically meaningless sequences
`src/config.py:263`; `src/models/pattern_detector.py:226-230, 258-262`

At 2 time steps: any non-constant pair correlates at exactly ±1 (dependency scores become coin flips), and a spike is *unreachable* below 4 steps (one outlier = fraction 0.5 > `PATTERN_SPIKE_MAX_DURATION_FRACTION` 0.25 ⇒ classified as sustained shift). Validation passes and nothing crashes (QA tests the boundary, `test:622-627`), but results at the floor are noise for several patterns. Either raise the floor (e.g. 4–10) or document in `detect()`'s docstring that scores below ~10 steps are low-signal. Config-only change; flagged for the tech-lead/Week-2 calibration pass.

### F6 — INFO: Gap-based confidence is 0.0 for the most extreme multi-pattern failures
`src/models/pattern_detector.py:190-201`; pinned by `test:767-775`

A sequence pinned far beyond range scores drift = saturation = cascade = 1.0, so confidence = 0.0 — the clearest catastrophic input yields the *lowest* confidence. This is inherent to the spec's top-2-gap formula (faithfully implemented and correctly documented as "how clearly the primary pattern stands out", not "how anomalous"), and the QA tie test pins the behavior. Consumers (Week 2 Confidence Scorer is next) must not gate alerting on this field alone; `primary_score` carries the magnitude. Worth one docstring sentence when next touched.

### F7 — INFO: Frozen dataclass holds mutable arrays
`src/models/pattern_scores.py:44-65`

`@dataclass(frozen=True)` prevents field rebinding but the ndarray contents remain writable; a buggy consumer could mutate baseline stats in place. No scorer mutates (verified), so this is aspirational hardening: set `.flags.writeable = False` in `compute_baseline` if desired.

### F8 — INFO: Org-rule deviations on file/test structure
`tests/models/test_pattern_detector.py` (789 lines) exceeds the 300-line cap — same standing note as Phase 1.1 (F8 there); the single-file-per-module test convention is project-wide and splitting is a QA/tech-lead call, not a correctness issue. A handful of tests target private methods (`_compute_confidence` at `test:521-527, 760-765`, `_explain_pattern` at `test:529-545`) — implementation-coupled, but they pin documented contract semantics (gap formula, templates) and are cheap to migrate if internals change. Both src modules respect the cap (268/299 lines) — the F8/N6 lesson from Phase 1.1 was correctly applied via the two-file split.

### F9 — INFO: Constant-baseline-channel corner makes drift and saturation overlap
`src/models/pattern_scores.py:93-101, 172-186`

If a baseline channel is perfectly constant (std = 0, range = 0), a sequence stuck at that same value is simultaneously "drifting" (var 0 < ratio·ε) and "saturated" (within 2%·ε of both bounds). Semantically defensible (it *is* at both min and max), can't occur with real sensor noise, and the tie resolves deterministically to drift. No action; recorded so future calibration isn't surprised.

---

## Test Quality Summary

Strong. All 84 tests are seeded (`np.random.default_rng(42)` via fixture), independent (function-scoped fixtures, no shared mutable state, cross-instance determinism explicitly tested at `test:777-789`), and behavior-focused with descriptive names. Coverage of the review-mandated categories:

- **Edge cases:** empty baseline, wrong rank, sensor mismatch, NaN/±Inf at both boundaries, exact `PATTERN_MIN_SEQ_LEN`, empty batch, list inputs, baseline re-set, constant channels at baseline *and* detect time, zero FFT power, short-baseline spectra. The spike-duration boundary is tested **at** and **one-past** the threshold (`test:733-749`) — exactly the off-by-one discipline the org rules ask for.
- **Per-pattern positive/negative/isolation:** every scorer has a triggering case, a non-triggering case, and an affected-sensor isolation case, including the negative-shift lag direction (`test:751-758`).
- **Error contract:** all `ValueError` paths asserted with `pytest.raises(match=...)`.
- **Bug tracking:** the strict xfail (`test:675-692`) is exemplary — root cause, suggested fix, and strictness so it cannot silently rot. The dead-guard coverage technique (hand-built `(1,1)`-correlation baseline, `test:649-673`) legitimately exercises the otherwise-unreachable branch without touching src.

Gap to close with F1's fix: promote the xfail and add a 2-sensor minimal-pair regression test for `score_dependency` through the public path.

---

## Verification Performed (read-only reviewer)

- Module suite independently re-run: `venv/bin/python -m pytest tests/models/test_pattern_detector.py -q --no-cov` → **83 passed, 1 xfailed in 0.08s** (matches orchestrator/QA exactly). Full-gate figures (627 passed, TOTAL 94.31%) taken from the orchestrator's verified run, consistent with the coverage doc.
- Single-sensor crash independently reproduced (0-d `correlations` confirmed; `ValueError: input array must be 2-d` at `triu_indices_from`) and the `np.atleast_2d` fix verified to restore correct `(0.0, [])` scoring through the existing guard.
- Secrets/red-flag grep over all four files: clean. Import graph verified one-directional with no torch/API edges.

## Required for follow-up (non-blocking)

1. F1: `np.atleast_2d` in `_safe_corrcoef` + xfail promotion — next phase touching these modules.
2. F2: `len(x) // 4` → config constant — fold into the same change.

Recommended: F4 batch-shape guard, F5 floor/docstring decision at Week-2 calibration, F3 z-score helper opportunistically.

---

*Reviewed 2026-06-13 by code-reviewer (Phase 1.9.1 Week 1 Phase 1.2 gate).*

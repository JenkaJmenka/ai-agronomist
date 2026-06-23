# Phase 3.1 Active Learning — Code Review

**Date:** 2026-06-15
**Reviewer:** code-reviewer (Claude Opus)
**Verdict:** **NEEDS FIXES**

## Files Reviewed

| File | Lines | Role |
|------|-------|------|
| `src/models/feedback_collector.py` | 181 | Circular-buffer feedback store |
| `src/models/feedback_validator.py` | 151 | EMA operator reliability tracker |
| `src/models/active_learner.py` | 295 | Retrain decision + per-customer learner |
| `src/models/retraining_orchestrator.py` | 208 | Facade for API layer |
| `src/api/routes/feedback.py` | 151 | FastAPI async endpoints |
| `src/api/schemas/feedback.py` | 166 | Pydantic v2 request/response DTOs |
| `tests/models/test_active_learner.py` | 1440 | 109 tests, 15+ classes |

---

## STOP-WORK Guardrail Results

### 1. No tight coupling between LSTM-VAE / model weights and feedback/learning modules — ✅ PASS

None of the 6 production files import `torch`, `lstm_vae`, `anomaly_embedding`, or
any model-weight module. Import chains:

- `feedback_collector.py` → `src.config` only
- `feedback_validator.py` → `src.config` only
- `active_learner.py` → `src.config`, `feedback_collector`, `feedback_validator`
- `retraining_orchestrator.py` → `src.config`, `active_learner`
- `routes/feedback.py` → `retraining_orchestrator`, `schemas/feedback`, `db/session`
- `schemas/feedback.py` → `pydantic` only

Direction is strictly one-way: `api → models → config`. No circular imports
(`grep -rn "from src.api" src/models/` returns zero hits). Clean.

### 2. No secrets or API keys in code — ✅ PASS

Grep for `password`, `secret`, `api_key`, `token`, `API_KEY`, `SECRET`, `PASSWORD`
across all 7 files: **zero hits** (excluding Pydantic field `description` strings).
No `eval()`, `exec()`, `subprocess`, or `os.system` imports found.

### 3. No hardcoded thresholds — all from `src/config.py` — ⚠️ PASS WITH NOTES

All major tuning thresholds correctly reference `ACTIVE_LEARNING_*` config constants:
capacity (1000), min quality labels (20), operator accuracy threshold (0.85), min
pattern types (2), retrain interval hours (4), EMA alpha (0.2), default reliability
(0.5), max/min training samples (100/10), improvement threshold (0.05).

**Violations (WARN):**

| Value | Location | Issue |
|-------|----------|-------|
| `days=30` | `active_learner.py:49`, `:275` | Hardcoded feedback lookback window. No `ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS` exists in config. |
| `0.8` | `active_learner.py:124`, `retraining_orchestrator.py:62` | Hardcoded default `operator_confidence`. No config constant exists. |

**Acceptable (not violations):**

- `"phase": 1`, `"risk_level": "low"` — domain-intrinsic metadata describing the
  Phase 1 retrain plan, not tuning thresholds.
- `count = 0` — loop counters, not configurable values.

### 4. Test coverage ≥ 90% on all 4 model files — ✅ PASS

QA reports **100% statement AND branch coverage** on all four model files:

| Module | Statements | Missing | Coverage |
|--------|-----------|---------|----------|
| `feedback_collector.py` | all | 0 | 100% |
| `feedback_validator.py` | all | 0 | 100% |
| `active_learner.py` | all | 0 | 100% |
| `retraining_orchestrator.py` | all | 0 | 100% |

109 tests in `test_active_learner.py`, all passing (0.77s). Gate: 296 existing
tests passed, 1 xfailed, no regressions.

### 5. No magic numbers inline in model files — ⚠️ PASS WITH NOTES

Same `days=30` and `0.8` as check #3. All other numeric values either come from
config or are semantic constants (`"phase": 1`).

---

## Red Flags Check (`.claude/guardrails/red-flags.md`)

| # | Red Flag | Result |
|---|----------|--------|
| 1 | File writing outside ownership | ✅ All files in `src/models/`, `src/api/`, `tests/` per ownership table |
| 2 | `os.system`, `subprocess`, `eval()` | ✅ None found |
| 3 | Hardcoded password/token/key | ✅ None found |
| 4 | DELETE/DROP without tenant_id | ✅ N/A — no database mutations |
| 5 | model.save() without metadata | ✅ N/A — no model saving |
| 6 | Test imports + modifies implementation | ✅ Test file is read-only |
| 7 | Circular imports models ↔ api | ✅ One-way: api → models only |

---

## Universal Guardrails Check (`.claude/guardrails/universal-guardrails.md`)

| Check | Result |
|-------|--------|
| Tight coupling LSTM-VAE ↔ API | ✅ None |
| Secrets hardcoded | ✅ None |
| Checkpoint without metadata | ✅ N/A |
| DB schema without tenant_id | ✅ N/A (in-memory per-customer dict) |
| Test coverage < 80% | ✅ 100% |
| Major design change without ADR | ✅ N/A |

**WARN items:**

| WARN | Status |
|------|--------|
| Hardcoded thresholds | ⚠️ `days=30`, `0.8` default (see check #3) |
| Missing error handling on external calls | ⚠️ See F2 below |
| Code duplication (3+ blocks) | ✅ Clean — no 3+ identical blocks |
| Silent failures (bare except, pass) | ✅ None found |

---

## ML Guardrails Check (`.claude/guardrails/ml-guardrails.md`)

| Check | Result |
|-------|--------|
| Hard-coded sensor ranges | ✅ N/A — no sensor ranges in these modules |
| LSTM-VAE spec changes | ✅ N/A — no LSTM-VAE modifications |
| Checkpoint without metadata | ✅ N/A — Phase 1 retrain is simulated |
| NaN/Inf in model outputs | ⚠️ See F3 below |

---

## QA Bug Triage

### BUG-1: RetrainingDecisionMaker.should_retrain signature mismatch — **NON-BLOCKING**

**QA claim:** "receives FeedbackCollector but internally calls `.get_recent()` +
`.get_high_quality()` — any signature mismatch silently passes None checks."

**Reviewer finding:** The QA description is **inaccurate**. `should_retrain`
(lines 35–70) calls:
- `feedback_collector.get_recent(days=30)` — valid method on `FeedbackCollector` ✅
- `validator.get_operator_reliability(op_id)` — valid method on `FeedbackValidator` ✅

It does **NOT** call `.get_high_quality()`. It manually filters `recent` feedback
by operator reliability. Both parameter types are correctly annotated. The actual
call path through `ActiveLearner._should_retrain_now()` always passes the correct
types.

There is a **real but separate** inconsistency: `FeedbackCollector.get_high_quality()`
filters by the `quality_score` field (set externally, defaults to 0.0), while
`should_retrain` and `_count_high_quality` filter by `validator.get_operator_reliability()`.
These are two different "quality" concepts — the `quality_score` field on feedback items
is never updated by any code in this module, making `get_high_quality()` effectively
dead code in the current call graph. This is a design gap (N3 below), not a
blocking bug.

### BUG-2: ActiveLearner.collect_operator_feedback returns learning_triggered=True but never calls trigger_retraining() — **BLOCKING**

**QA claim confirmed.** `collect_operator_feedback` (lines 117–166) sets
`is_learning_triggered = self._should_retrain_now()` and returns it in the dict,
but does NOT invoke `self.trigger_retraining()`.

**Current mitigation:** `RetrainingOrchestrator.observe()` (lines 86–101) correctly
checks `result["learning_triggered"]` and calls `self._learner.trigger_retraining()`.
All API routes use `RetrainingOrchestrator`, so the production path is safe **today**.

**Why BLOCKING:** The `ActiveLearner` class is a public API (`__init__.py` exports
it, tests construct it directly, it has full docstrings and type annotations). Its
`collect_operator_feedback` method:

1. Returns `learning_triggered=True` but the caller has no compile-time obligation
   to act on it — a fire-and-forget caller silently skips retraining.
2. `_last_retrain` is never updated when used bare, so the cooldown in
   `_should_retrain_now()` never fires. Every subsequent call returns
   `learning_triggered=True` — an **infinite trigger-without-action loop**.
3. The method docstring says "check if learning should trigger" but doesn't
   document that callers MUST call `trigger_retraining()` themselves.

**Fix options (pick one):**
- **(A)** Have `collect_operator_feedback` call `trigger_retraining()` itself when
  criteria are met (simplest; mirrors `RetrainingOrchestrator.observe()`).
- **(B)** Add explicit `@property` or method that returns a "should retrain" flag
  without the side-effect expectation, and document the intended usage contract.
- **(C)** Make `ActiveLearner` private (`_ActiveLearner`) so callers must go through
  `RetrainingOrchestrator`.

### BUG-3: get_statistics() builds operator_reliability from feedback buffer, not from FeedbackValidator — **NON-BLOCKING**

**QA claim confirmed.** `_get_operator_ids()` (lines 286–294) iterates the circular
`deque`, not `self.validator.operator_ids`. When the buffer capacity (1000) is
exceeded, evicted operators disappear from `get_statistics()['operator_reliability']`
despite their EMA scores being retained in the validator.

**Impact:** This affects the `/api/feedback/statistics/{customer_id}` reporting
endpoint only. Retraining decisions (`should_retrain`, `_count_high_quality`,
`_get_training_feedback`) operate on the current buffer contents + validator, so
retraining correctness is unaffected.

**Risk:** Low — with capacity=1000 and typical operator pools, eviction won't
happen frequently. The inconsistency is in a reporting path, not a decision path.

**Recommended fix:** Union `self.feedback_collector` operator IDs with
`self.validator.operator_ids` in `_get_operator_ids()`.

---

## Findings (beyond QA bugs)

### F1 — `validate_and_promote` accepts `float('inf')` and `float('nan')` — WARN

`RetrainingOrchestrator.validate_and_promote()` (lines 138–202) checks:
```python
if not isinstance(model_improvement_pct, (int, float)): ...
if model_improvement_pct < 0.0: ...
```

Both `float('inf')` and `float('nan')` pass `isinstance(..., float)`.
`float('inf') >= 0.0` is `True` → silently promotes with infinite improvement.
`float('nan') >= 0.0` is `False` → raises ValueError (accidental correctness).

**Fix:** Add `math.isfinite(model_improvement_pct)` check after the isinstance guard.

### F2 — `db: AsyncSession = Depends(get_db)` injected but never used — WARN

All four API endpoints in `routes/feedback.py` accept `db: AsyncSession = Depends(get_db)`
but never reference `db`. The endpoints operate entirely on in-memory `_orchestrators`.

**Impacts:**
1. Unnecessary database connection opened and closed per request (performance).
2. Misleading to future maintainers — suggests DB integration exists when it doesn't.
3. When persistence is added later, the injection is pre-wired (positive).

**Recommendation:** Remove `db` parameter from all four endpoints now; re-add when
persistence is implemented. Or add a `# TODO` comment explaining the intent.

### F3 — No `float('inf')`/`float('nan')` guard on `operator_confidence` in model layer — INFO

`FeedbackCollector._validate_collect_inputs` checks `0.0 <= operator_confidence <= 1.0`,
which correctly rejects `nan` (comparison with `nan` is `False`) but accepts values
that pass the range check. `float('inf')` fails (> 1.0). No issue in current code,
but the Pydantic schema has `ge=0.0, le=1.0` which also correctly rejects inf/nan
at the API boundary. Defense in depth is adequate.

### F4 — `_orchestrators` module-level dict is not thread-safe — INFO

`routes/feedback.py` line 37: `_orchestrators: Dict[str, RetrainingOrchestrator] = {}`.
In async FastAPI with single worker, concurrent coroutine interleaving around
`_get_orchestrator` could theoretically create two orchestrators for the same
customer (dict check + assignment is not atomic). Low risk in practice because:
1. Python's GIL prevents true data races on dict operations.
2. Both orchestrators would be valid; the last one wins in the dict.
3. This is explicitly documented as "In production: use Redis or a persistent store."

### F5 — `feedback` property exposes mutable internal state — INFO

`FeedbackCollector.feedback` (line 149) returns the internal `deque` directly.
External callers (including tests) can mutate it:
```python
collector.feedback[0]["quality_score"] = 0.9  # in tests
```
This breaks encapsulation. The docstring says "Read-only access" but the deque itself
is mutable. Acceptable for Phase 1 / testing convenience, but consider returning a
`tuple()` copy or `types.MappingProxyType` wrapper for production.

### F6 — `import time` unused in test file — INFO

`tests/models/test_active_learner.py` line 9: `import time` is never referenced.
Dead import.

### F7 — `learning_progress` returns misleading `samples_needed=0` for unknown customers — INFO

`routes/feedback.py` lines 113–124: When a customer has no orchestrator, the endpoint
returns `samples_needed=0`, suggesting the retrain target is already met. The correct
value would be `ACTIVE_LEARNING_MIN_QUALITY_LABELS` (20), indicating all samples are
still needed. This is a reporting-only issue; no retraining logic is affected.

### F8 — `quality_score` field on feedback items is effectively dead code — INFO

`FeedbackCollector.collect()` initializes `"quality_score": 0.0` on every feedback
item (line 104). `get_high_quality()` filters by this field. However, no code in the
active learning module ever updates `quality_score` after collection. The docstring
says "Quality scores are set externally by FeedbackValidator," but no such update
mechanism exists.

The actual quality filtering used by `should_retrain`, `_count_high_quality`, and
`_get_training_feedback` all go through `validator.get_operator_reliability()` directly.
The `quality_score` field and `get_high_quality()` method are unused in the real
pipeline.

---

## Security Review

| Check | Result |
|-------|--------|
| Input validation at boundaries | ✅ All inputs validated (empty strings, ranges, types) |
| Pydantic v2 schema validation | ✅ `min_length`, `ge`, `le` constraints on all fields |
| No eval/exec/subprocess | ✅ Clean |
| No secrets in source | ✅ Clean |
| No SQL injection vectors | ✅ No SQL in these modules |
| Auth on endpoints | ⚠️ None — known Phase 1 limitation |
| Logging — no PII/secrets | ✅ Logs only: incident_id, operator_id, is_anomaly, reliability % |

---

## Performance Review

| Aspect | Assessment |
|--------|-----------|
| Buffer operations | O(n) where n ≤ capacity (1000) — acceptable |
| `_count_high_quality` | Full deque scan on every `collect_operator_feedback` call — O(n) per feedback submission. Consider incremental counter for production scale. |
| `get_recent` | Full deque scan with datetime comparison — O(n), acceptable |
| Memory | In-memory only, one dict + deque + validator per customer. No persistence = state lost on restart. |
| DB connection | Opened but unused per request (F2) — wasted connection pool slot |

---

## Type Hints Review

| Aspect | Assessment |
|--------|-----------|
| Return types on all public APIs | ✅ All annotated |
| Parameter types | ✅ All annotated |
| `Dict[str, Any]` overuse | ⚠️ 20+ instances — consider `TypedDict` for structured return values |
| `sequence: Any` | ⚠️ Intentionally untyped (future retraining data); document expected types |
| Pydantic models | ✅ Strong typing with `Field` constraints |

---

## Test Quality Review

| Aspect | Assessment |
|--------|-----------|
| Coverage | ✅ 100% statement + branch on all 4 model files |
| Test count | ✅ 109 tests across 15+ classes |
| Edge cases | ✅ Boundary values, empty collections, type errors, eviction |
| Independence | ✅ No test-on-test dependencies |
| Naming | ✅ Descriptive (`should_return_False_with_less_than_MIN_QUALITY_LABELS`) |
| Private access | ⚠️ Tests access `_last_retrain`, `_should_retrain_now()` directly — acceptable for white-box testing but fragile |
| Dead import | ⚠️ `import time` (line 9) unused |
| Regression | ✅ 296 existing tests pass, 1 xfailed, no regressions |

---

## Summary

### Blocking Issues (1)

1. **BUG-2**: `ActiveLearner.collect_operator_feedback` returns `learning_triggered=True`
   without calling `trigger_retraining()`. Bare `ActiveLearner` callers silently skip
   retraining and enter an infinite trigger loop (cooldown never set). Fix required
   before this API is documented/published.

### Non-Blocking Issues (2 bugs + 8 findings)

| ID | Severity | Description |
|----|----------|-------------|
| BUG-1 | INFO | QA description inaccurate — no actual signature mismatch. `get_high_quality()` vs `validator.get_operator_reliability()` conceptual inconsistency (see F8). |
| BUG-3 | WARN | `get_statistics()` operator list built from buffer, not validator — evicted operators vanish from stats. Reporting-only impact. |
| F1 | WARN | `validate_and_promote` accepts `float('inf')` — add `math.isfinite()` check. |
| F2 | WARN | `db: AsyncSession` injected but unused in all 4 feedback routes. |
| F3 | INFO | `operator_confidence` inf/nan — adequately guarded by Pydantic at API boundary. |
| F4 | INFO | Module-level `_orchestrators` dict — thread-safety caveat for production. |
| F5 | INFO | `feedback` property exposes mutable deque despite "read-only" docstring. |
| F6 | INFO | Dead `import time` in test file. |
| F7 | INFO | `learning_progress` returns `samples_needed=0` for unknown customers. |
| F8 | INFO | `quality_score` field + `get_high_quality()` effectively dead code — never updated. |

### Hardcoded Values (WARN per universal guardrails)

| Value | Location | Recommended Config Constant |
|-------|----------|-----------------------------|
| `days=30` | `active_learner.py:49,275` | `ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS` |
| `0.8` | `active_learner.py:124`, `retraining_orchestrator.py:62` | `ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE` |

---

## Verdict: **NEEDS FIXES**

BUG-2 is a blocking design flaw: a public class with a documented API returns a
signal (`learning_triggered=True`) that it doesn't act on, creating a silent failure
mode and infinite trigger loop for any caller that doesn't manually invoke
`trigger_retraining()`. The production path through `RetrainingOrchestrator` is safe
today, but the exposed `ActiveLearner` API is a trap.

**Required before proceeding:**
1. Fix BUG-2 — either auto-trigger retraining in `collect_operator_feedback`, or
   restrict `ActiveLearner` visibility so all callers go through `RetrainingOrchestrator`.

**Recommended (non-blocking):**
2. Extract `days=30` and `0.8` to config constants.
3. Add `math.isfinite()` guard in `validate_and_promote`.
4. Fix `learning_progress` to return correct `samples_needed` for unknown customers.
5. Remove unused `db` parameter from feedback routes (or add TODO).
6. Remove dead `import time` from test file.

# Phase 3.1 Active Learning — Re-Review (v2)

**Date:** 2026-06-15
**Reviewer:** code-reviewer (Claude Opus)
**Previous verdict:** NEEDS FIXES ([active_learning_review.md](active_learning_review.md))
**Verdict:** **PASS WITH NOTES**

---

## Scope

Re-check only the items from the fix cycle (BUG-2 blocking + 5 non-blocking fixes).
Quick regression scan for issues introduced by the fixes.
Unchanged files trusted from the v1 review.

---

## BUG-2 (was BLOCKING) — **VERIFIED CLOSED**

### Fix description
`ActiveLearner.collect_operator_feedback` now auto-calls `trigger_retraining()`
when `_should_retrain_now()` returns True.

### Verification checklist

| # | Check | Result |
|---|-------|--------|
| 1 | `trigger_retraining()` is called | ✅ Line 180: `retrain_result = self.trigger_retraining()` inside `if is_learning_triggered:` block |
| 2 | `_last_retrain` is set | ✅ `trigger_retraining()` sets `self._last_retrain = now` (line 220) on success |
| 3 | Infinite loop is gone | ✅ After the first trigger, `_should_retrain_now()` returns `False` due to cooldown (`elapsed < cooldown`, line 272), breaking the previously infinite trigger-without-action loop |
| 4 | Return dict reflects retraining outcome | ✅ Line 181: `result["retraining"] = retrain_result` — callers see the retraining status without needing to act |

### Docstring updated correctly
Lines 132–137 now read: "When all retraining criteria are satisfied [...] retraining
is triggered automatically. The result dict includes the ``retraining`` key with the
retraining outcome when this happens." — Accurate, no misleading contract.

### `RetrainingOrchestrator.observe()` — no double-trigger
Lines 99–102 of `retraining_orchestrator.py`: the old separate
`self._learner.trigger_retraining()` call is correctly removed. The comment
explains: "collect_operator_feedback now auto-triggers retraining when criteria
are met, so the result already includes 'retraining'." The method now passes
the result through directly. ✅ No double-trigger.

### Test coverage of the fix
`test_learning_triggered_true_above_threshold` (test file lines 577–605):
- Asserts `result["learning_triggered"] is True` ✅
- Asserts `"retraining" in result` ✅
- Asserts `result["retraining"]["status"] == "retraining_completed"` ✅
- Asserts `learner._last_retrain is not None` ✅ (cooldown set)

`test_observe_triggers_retraining` (test file lines 743–766): full path through
`RetrainingOrchestrator` also verifies the `retraining` key is present. ✅

**Verdict on BUG-2: CLOSED.** The fix is clean, the infinite loop is eliminated,
and both the bare `ActiveLearner` and `RetrainingOrchestrator` paths are
verified by tests.

---

## Non-Blocking Fix 1 — Config constants added — **VERIFIED**

| Constant | Location | Value | Status |
|----------|----------|-------|--------|
| `ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS` | `config.py:449` | `30` | ✅ Added with `int` annotation and docstring comment |
| `ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE` | `config.py:452` | `0.8` | ✅ Added with `float` annotation and docstring comment |

Both constants are in the `Active Learning` config section, following the
existing naming convention (`ACTIVE_LEARNING_*`). ✅

---

## Non-Blocking Fix 2 — Hardcoded values replaced in `active_learner.py` — **VERIFIED**

| Old value | Location | New value | Status |
|-----------|----------|-----------|--------|
| `days=30` | Line 52 (was 49) | `days=ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS` | ✅ |
| `days=30` | Line 293 (was 275) | `days=ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS` | ✅ |
| `0.8` default | Line 128 (was 124) | `ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE` | ✅ |

Import added at line 14: `ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE` ✅
Import added at line 16: `ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS` ✅

Grep for remaining hardcoded `days=30` in this file: **zero hits**. ✅
Grep for remaining hardcoded `= 0.8` default in this file: **zero hits**. ✅

---

## Non-Blocking Fix 3 — `retraining_orchestrator.py` hardcoded `0.8` + `math.isfinite()` — **VERIFIED**

### Hardcoded `0.8` replaced
Line 64: `operator_confidence: float = ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE` ✅
Import at line 17: `ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE` ✅

### `math.isfinite()` guard added
Line 2 (well, 13): `import math` ✅
Lines 164–168:
```python
if not math.isfinite(model_improvement_pct):
    raise ValueError(
        "model_improvement_pct must be finite, "
        f"got {model_improvement_pct}"
    )
```

Check order is correct: `isinstance` → `isfinite` → `< 0.0`. ✅

- `float('inf')`: passes isinstance, **fails** isfinite → ValueError ✅
- `float('nan')`: passes isinstance, **fails** isfinite → ValueError ✅
- `float('-inf')`: passes isinstance, **fails** isfinite → ValueError ✅
- Normal floats: pass all three guards ✅

Docstring updated (line 149): "Must be finite and >= 0." ✅

### Test gap (minor, INFO)
No tests in `test_active_learner.py` exercise `validate_and_promote(float('inf'))` or
`validate_and_promote(float('nan'))`. The guard is straightforward and correct, but
adding parametrized tests for inf/nan would close the coverage gap on the new code path.
Other modules in this project (e.g., `test_confidence_scorer.py`) do test
inf/nan rejection parametrically.

---

## Non-Blocking Fix 4 — `feedback.py` endpoint fixes — **VERIFIED**

### `learning_progress` returns correct `samples_needed` for unknown customers
Lines 111–118: Unknown customer now returns:
```python
samples_needed=ACTIVE_LEARNING_MIN_QUALITY_LABELS
```
Previously returned `samples_needed=0` (misleading). ✅

Import at line 28: `ACTIVE_LEARNING_MIN_QUALITY_LABELS` ✅
The value (20) correctly indicates all samples are still needed. ✅

### Unused `db` parameter removed
All four endpoints (`mark_feedback`, `feedback_statistics`, `learning_progress`,
`record_outcome`) no longer accept `db: AsyncSession = Depends(get_db)`. ✅

Imports cleaned up: no `Depends`, no `get_db`, no `AsyncSession` in the import
block. ✅

Module-level TODO comment at line 13:
```python
# TODO: add db persistence — currently all state is in-memory per-customer
```
This documents the intent for future persistence. ✅

---

## Non-Blocking Fix 5 — Dead `import time` removed from test file — **VERIFIED**

Line 9 of `tests/models/test_active_learner.py` is now:
```python
from datetime import datetime, timedelta, timezone
```
No `import time` anywhere in the file. ✅

---

## Regression Scan

### Tests
109 tests in `tests/models/test_active_learner.py` — **all 109 passed** (0.08s). ✅

### Code inspection for regressions introduced by fixes

| Check | Result |
|-------|--------|
| Double-retraining via `RetrainingOrchestrator.observe()` | ✅ Eliminated — old trigger call removed, comment explains delegation |
| `trigger_retraining()` returning `insufficient_data` after auto-trigger | ✅ Handled correctly — result dict includes whatever `trigger_retraining()` returns, including insufficient_data status. Unlikely in practice (same feedback satisfying `should_retrain` criteria is also the training pool) but the edge case is correctly propagated |
| `_last_retrain` set only on successful retrain | ✅ Only set inside `trigger_retraining()` on the success path (line 220), not on `insufficient_data` return — correct behavior: failed retrain doesn't consume cooldown |
| Config constant values match previously hardcoded values | ✅ `FEEDBACK_WINDOW_DAYS=30` matches old `days=30`; `DEFAULT_OPERATOR_CONFIDENCE=0.8` matches old `0.8` |
| Import chain still one-way (api → models → config) | ✅ No new circular imports introduced |
| No new hardcoded values introduced | ✅ Quick scan of all 5 modified files — clean |
| `isfinite` guard doesn't break existing valid inputs | ✅ All ints and normal floats pass `math.isfinite()` |

### No new issues found
The fixes are surgical and do not introduce regressions. The code is cleaner
than before: the `ActiveLearner` API contract is now self-contained (callers
don't need to know to call `trigger_retraining()` separately), config
centralization is complete for the active learning module, and the
`validate_and_promote` guard is defense-in-depth.

---

## Remaining non-blocking items from v1 (unchanged, carried forward)

These items from the v1 review were NOT in scope for the fix cycle and remain
as-is. They are non-blocking and tracked for future work:

| ID | Severity | Description | Status |
|----|----------|-------------|--------|
| BUG-1 | INFO | QA description inaccurate — `quality_score` vs `validator.get_operator_reliability()` conceptual gap (F8). | Unchanged — design gap, not a runtime bug |
| BUG-3 | WARN | `get_statistics()` operator list from buffer, not validator — evicted operators vanish. | Unchanged — reporting-only impact |
| F3 | INFO | `operator_confidence` inf/nan — adequately guarded at API boundary by Pydantic. | Unchanged — defense in depth adequate |
| F4 | INFO | Module-level `_orchestrators` dict thread-safety caveat. | Unchanged — documented, low risk in async single-worker |
| F5 | INFO | `feedback` property exposes mutable deque despite "read-only" docstring. | Unchanged — acceptable for Phase 1 |
| F8 | INFO | `quality_score` field + `get_high_quality()` effectively dead code. | Unchanged — no production impact |

### New minor note (from this review)

| ID | Severity | Description |
|----|----------|-------------|
| N1 | INFO | No tests for `validate_and_promote(float('inf'))` or `float('nan')` after adding `math.isfinite()` guard. Consider adding parametrized tests (see `test_confidence_scorer.py` for pattern). |

---

## Verdict: **PASS WITH NOTES**

All blocking and non-blocking fixes from the v1 review have been verified as
correctly implemented:

1. **BUG-2 CLOSED**: `collect_operator_feedback` now auto-triggers retraining,
   sets cooldown, and returns the outcome. No infinite loop. No double-trigger
   through `RetrainingOrchestrator`. Fully tested.
2. **Config constants**: Both `ACTIVE_LEARNING_FEEDBACK_WINDOW_DAYS` and
   `ACTIVE_LEARNING_DEFAULT_OPERATOR_CONFIDENCE` added and used everywhere
   the hardcoded values appeared.
3. **`math.isfinite()` guard**: Correctly rejects inf/nan in `validate_and_promote`.
4. **`learning_progress` endpoint**: Returns correct `samples_needed` for
   unknown customers.
5. **`db` parameter**: Removed from all 4 endpoints with a TODO for future
   persistence.
6. **Dead import**: `import time` removed from test file.

109 tests pass. No regressions introduced. The active learning module's public
API contract is now safe for direct use.

**The Phase 3.1 Active Learning module is approved for integration wiring.**

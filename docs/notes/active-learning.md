# Active Learning — Notes

**Source:** active_learning_review.md, active_learning_review_v2.md, phase4_integration_review.md

## NOTE-AL-001: No tests for validate_and_promote(inf/nan)

**Resolved** 2026-06-15 — parametrized tests added in `TestRetrainingOrchestratorValidateAndPromote::test_non_finite_improvement_raises`.

---

## NOTE-AL-002: feedback property exposes mutable deque

`ActiveLearner.feedback` property returns the internal deque despite "read-only" docstring. Callers could mutate buffer.

**Mitigation:** Return `tuple()` copy or document as intentional for Phase 1.  
**Effort:** 30 min if fixing

---

## NOTE-AL-003: _orchestrators thread-safety

Module-level dict not thread-safe. Low risk in async single-worker uvicorn; problematic under multi-worker gunicorn.

**Mitigation:** Document single-worker requirement until BUG-008 fixed.  
**See also:** [BUG-008](../bugs/BUG-008-orchestrator-state-not-persistent.md)

---

## NOTE-AL-004: operator_confidence inf/nan at model layer

Pydantic guards at API boundary. Model layer accepts any float. Defense-in-depth adequate for Phase 1.

**Optional:** Add `math.isfinite()` in `FeedbackCollector.collect()`.

---

## NOTE-AL-005: learning_progress null until first feedback

Comprehensive inference returns `learning_progress: null` for customers with no prior feedback. By design — not a bug.

**Optional UX:** Return zeros struct instead of null for unknown customers.

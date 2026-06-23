# BUG-002: ActiveLearner never auto-triggered retraining (CLOSED)

**Status:** Closed — 2026-06-15  
**Severity:** Was blocking  
**Fixed in:** Phase 3.1 fix cycle  
**Source:** [active_learning_review.md](../review/active_learning_review.md), [active_learning_review_v2.md](../review/active_learning_review_v2.md)

## Summary

`ActiveLearner.collect_operator_feedback()` returned `learning_triggered=True` but never called `trigger_retraining()`, causing infinite trigger loops and no cooldown.

## Fix applied

`collect_operator_feedback` now auto-calls `trigger_retraining()` when criteria met. `RetrainingOrchestrator.observe()` no longer double-calls. Verified in review v2.

## Do not reopen

Unless regression is found in `src/models/active_learner.py` lines 179–181.

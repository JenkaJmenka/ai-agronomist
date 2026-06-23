# BUG-008: _orchestrators dict not persistent across restarts

**Status:** Closed (2026-06-15)  
**Severity:** Medium (production)  
**Source:** Phase 3.1 review (F4), Phase 4 handover

## Summary

Per-customer `RetrainingOrchestrator` instances live in module-level `_orchestrators: Dict[str, RetrainingOrchestrator]`. Process restart loses all feedback history, operator reliability EMA state, and retraining cooldown timestamps.

## Impact

Active learning loop resets on every deploy/restart. Operators must re-submit feedback to reach retrain threshold.

## Fix (implemented)

DB-backed `learning_states` table with in-memory cache:

- `src/db/models.py` — `LearningState` model
- `alembic/versions/005_add_learning_state_table.py`
- `src/db/learning_state_store.py` — load/save
- `src/api/services/orchestrator_registry.py` — cache + hydration
- `export_state()` / `load_state()` on `ActiveLearner`, `RetrainingOrchestrator`

## Files

- `src/api/routes/feedback.py`
- `src/api/routes/feedback_integrated.py`
- `src/api/services/comprehensive_pipeline.py` (uses registry cache)

## Acceptance criteria

- [x] Feedback survives process restart
- [x] Cooldown `_last_retrain` persisted
- [x] Migration if DB-backed
- [x] Integration test: submit feedback → cache clear → state restored

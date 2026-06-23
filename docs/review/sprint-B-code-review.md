# Sprint B Code Review

**Reviewer:** code-reviewer (Claude Opus)
**Date:** 2026-06-21
**Verdict:** PASS WITH NOTES

---

## Summary

Sprint B delivers 10 focused changes spanning config centralization, fault taxonomy completion, ensemble performance optimizations, database safety improvements, and API response-model hardening. All changes are correct, introduce no new security vulnerabilities, and align with project guardrails. Two minor notes identified; no blocking issues.

---

## File-by-File Review

### 1. `src/models/gas_anomaly_detector.py`

**Change:** `GAS_COLUMNS` imported from `src.config` (no local definition); EWMA fully removed.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | Import alias `GAS_SENSOR_COLUMNS as GAS_COLUMNS` preserves all internal references without rename churn. EWMA removal is clean — no orphan imports or dead code. |
| Security | PASS | No issues. |
| Code quality | PASS | Well-documented, constants use `_UPPER_SNAKE` for module-private. Functions are focused and under 50 lines. |
| Consistency | PASS | Matches project patterns (dataclass DTOs, explicit type hints). |

**Note (non-blocking):** Line 60 — `GAS_COLUMNS: list[str] = GAS_COLUMNS` class attribute shadows the module-level import. The class methods reference the module-level name, so the class attribute is unused within the class itself. It does serve as a public accessor (`GasAnomalyDetector.GAS_COLUMNS`) but no caller uses that form today. Consider removing if it stays unused.

---

### 2. `src/models/fault_classifier.py`

**Change:** `FaultType.UNKNOWN` enum member added; fallthrough now logs warning and returns `unknown`.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | Fallthrough to `UNKNOWN` with `logger.warning` is the correct pattern — no more silent classification gaps. Priority ordering (bias > stuck > spike > drift > noise > unknown) is maintained. |
| Security | PASS | No issues. |
| Code quality | PASS | Stateless classifier, single-responsibility, under 50 lines. Log message includes the unmatched `pattern_type` for debugging. |
| Consistency | PASS | `str, Enum` pattern matches Pydantic v2 compatibility requirements. |

---

### 3. `src/models/ensemble_detector.py`

**Change:** Identity check replacing `np.array_equal` in baseline cache; vectorized range-violation loop; `assert` replaced with `RuntimeError`.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | Identity check (`baseline is not self._baseline_cache`, line 307) is semantically correct for cache invalidation — same array object means same data, skip re-fit. Avoids O(n) deep comparison cost of `np.array_equal`. |
| Correctness | PASS | Vectorized range violations (lines 212-214): `np.sum((sequence < mins) \| (sequence > maxs))` leverages numpy broadcasting correctly. `mins`/`maxs` are 1D arrays that broadcast against `(seq_len, ncols)` shape. Result is equivalent to the prior per-column loop. |
| Correctness | PASS | `RuntimeError` at lines 143, 181, 208 replaces `assert`. Assertions are stripped by `python -O`, so RuntimeError is the correct production guard for "detector not fitted" invariants. |
| Security | PASS | No issues. |
| Code quality | PASS | Good use of Protocol for detector interface. Helper `_clip_unit` and `_normalize_error` are focused utilities. |
| Consistency | PASS | Matches project patterns. |

---

### 4. `src/api/services/comprehensive_pipeline.py`

**Change:** `_save_trackers` uses savepoint via `begin_nested()`.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | `async with db.begin_nested():` creates a SAVEPOINT. On exception, only the savepoint rolls back — the outer transaction is untouched. This correctly isolates non-critical tracker writes from the main pipeline transaction. |
| Security | PASS | No secrets. Tenant isolation maintained via `customer_id` filter. |
| Code quality | PASS | The change is minimal and well-commented (lines 553-556 explain savepoint behavior). |
| Consistency | PASS | Test updated to verify `mock_db.rollback.assert_not_awaited()` reflecting the new savepoint contract. |

---

### 5. `src/db/learning_state_store.py`

**Change:** `.with_for_update()` on SELECT in `save()`.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | `SELECT ... FOR UPDATE` prevents concurrent upserts from causing last-write-wins races. On SQLite this is a no-op (acceptable for dev); on PostgreSQL it acquires a row-level lock. The merge strategy (`merged.update(state)`) preserves data from other writers (e.g., EWMA/POT trackers). |
| Security | PASS | No issues. |
| Code quality | PASS | Under 60 lines. Comment on lines 50-52 explains the merge rationale. |
| Consistency | PASS | Follows existing SQLAlchemy async patterns. |

---

### 6. `src/api/schemas/incidents.py`

**Change:** `fault_type` field_validator added to `IncidentCreateRequest`.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | `_VALID_FAULT_TYPES: ClassVar[frozenset[str]]` is built from the `FaultType` enum dynamically — any future enum additions are automatically included. `ClassVar` prevents Pydantic from treating it as a model field. Validator correctly passes `None` through (field is optional). |
| Security | PASS | Input validation at the API boundary — defense-in-depth alongside the DB CHECK constraint. Error message enumerates valid values (sorted) to aid debugging without leaking internals. |
| Code quality | PASS | Follows the existing `@field_validator("embedding")` pattern in the same class. |
| Consistency | PASS | Uses Pydantic v2 `@field_validator` + `@classmethod` decorator stack correctly. |

---

### 7. `alembic/versions/011_add_fault_type_constraint.py`

**Change:** New CHECK constraint migration on `incidents.fault_type`.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | Constraint values (`calibration_drift`, `bias_offset`, `stuck_fault`, `accuracy_decline`, `spike_fault`, `unknown`) exactly match `FaultType` enum members. `fault_type IS NULL` clause correctly handles the optional field. Revision chain (`011` -> `010`) is linear. |
| Security | PASS | DB-level constraint provides a second validation layer even if API validation is bypassed (direct DB access, migration scripts, etc.). |
| Code quality | PASS | Clean separation of `_VALID_VALUES` tuple and `_CHECK_EXPR` construction. Named constraint `ck_incidents_fault_type` enables clean `downgrade()`. |
| Consistency | PASS | Follows existing Alembic migration patterns. |

**Note (informational):** `op.drop_constraint()` in `downgrade()` requires `ALTER TABLE ... DROP CONSTRAINT` support. SQLite does not support this natively — downgrades on SQLite would need table recreation via `op.batch_alter_table()`. This is a known SQLite limitation and acceptable given PostgreSQL is the production target.

---

### 8. `src/api/schemas/gas.py`

**Change:** New Pydantic response models for gas endpoints.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | Five models (`GasReadingEntry`, `GasColumnSummary`, `GasSummaryResponse`, `GasRawEntry`, `GasReadingsResponse`) correctly mirror the dict structures returned by the route handlers. |
| Security | PASS | Response-only models — no input validation concern. |
| Code quality | PASS | Clean, minimal (~37 lines). Each model is focused. |
| Consistency | PASS | Follows Pydantic v2 patterns used in `incidents.py`. |

**Note (non-blocking):** `GasReadingEntry.timestamp` and `GasRawEntry.timestamp` are typed as `str` rather than `datetime`. The route serializes timestamps via `.isoformat()` before returning, so `str` is technically correct for the response shape. Using `datetime` would be more type-safe and let Pydantic handle serialization, but would require route changes. Acceptable as-is.

---

### 9. `src/api/routes/gas_anomalies.py`

**Change:** `response_model` wired to new schemas.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | `response_model=GasSummaryResponse` on `/summary` and `response_model=GasReadingsResponse` on `/readings` enable FastAPI to validate and document response shapes. Return dicts match model structures. |
| Security | PASS | Query params validated: `min_length=1` on tenant/greenhouse IDs, `ge=1, le=168` on hours window. `_source_is_real()` excludes synthetic readings. SQLAlchemy parameterized queries prevent injection. |
| Code quality | PASS | Helper `_fetch_real_readings` and `_source_is_real` are well-extracted. Route functions are focused. |
| Consistency | PASS | Follows existing route patterns (`APIRouter`, `Depends(get_db)`, `Query()` params). |

---

### 10. `src/config.py`

**Change:** `GAS_SENSOR_COLUMNS` canonical definition added.

| Criterion | Rating | Notes |
|-----------|--------|-------|
| Correctness | PASS | `GAS_SENSOR_COLUMNS: List[str] = ["c2h4", "gas_co2", "no", "no2", "nox", "co"]` is the single source of truth, imported by `gas_anomaly_detector.py` and indirectly by `gas_anomalies.py`. Eliminates the prior duplicated list. |
| Security | PASS | No secrets hardcoded. API keys sourced from environment variables. |
| Code quality | PASS | Follows the existing section-header pattern with clear documentation. |
| Consistency | PASS | Placed in the "Gas Sensor Detection (SCADA — Phase 4)" section alongside related constants. |

---

## Guardrail Compliance

### STOP WORK checks — all clear

| Guardrail | Status |
|-----------|--------|
| Tight coupling between LSTM-VAE and API layer | Not found |
| Secrets or API keys in code | Not found |
| Model checkpoint without metadata | N/A (no checkpoint changes) |
| Test coverage < 80% | 1206 passed per gate report |
| `eval()`, `subprocess(shell=True)`, `os.system` | Not found |
| DELETE/DROP without tenant_id filter | Not found |
| Circular imports between `src/models/` and `src/api/` | Not found |
| Hard-coded sensor ranges outside config.py | Not found |

### WARN checks

| Guardrail | Status |
|-----------|--------|
| Hard-coded thresholds | None introduced — `GAS_SENSOR_COLUMNS` moved to config |
| Missing error handling | `_save_trackers` exception handling improved via savepoint |
| Code duplication | Reduced — `GAS_COLUMNS` centralized |
| Silent failures | None introduced — fallthrough now logs warning |

---

## Cross-File Consistency

| Concern | Status |
|---------|--------|
| `FaultType` enum values match migration CHECK constraint | Verified — 6 values match exactly |
| `FaultType` enum values match `_VALID_FAULT_TYPES` in schema validator | Verified — built dynamically from enum |
| `GAS_SENSOR_COLUMNS` in config matches usage in detector + routes | Verified — single import chain |
| Savepoint contract matches test assertion (`rollback.assert_not_awaited`) | Verified |

---

## Test Quality Assessment

Sprint B test changes are limited to one assertion update in `test_comprehensive_pipeline_sprint_a.py`:
- `mock_db.rollback.assert_awaited_once()` changed to `mock_db.rollback.assert_not_awaited()` — correctly reflects the savepoint auto-rollback contract where `db.rollback()` is never called directly.

Existing test coverage for Sprint B changes:
- `TestFaultClassifier.test_default_unknown_fault` — covers `FaultType.UNKNOWN` fallthrough
- `TestGasAnomalyDetector` — 7 tests covering fit/detect including edge cases
- `TestSaveTrackersDBException` — covers savepoint isolation
- `TestLoadTrackersDBException` — covers `with_for_update()` path indirectly

**Missing tests (non-blocking):**
- No dedicated tests for the new `gas.py` response models (Pydantic model validation). These are exercised indirectly through the route integration tests, but explicit model tests would catch schema drift earlier.
- No migration test verifying the CHECK constraint rejects invalid values at the DB level.

---

## Findings Summary

| # | Severity | File | Line | Finding |
|---|----------|------|------|---------|
| 1 | NOTE | `gas_anomaly_detector.py` | 60 | Class attribute `GAS_COLUMNS` shadows module import and is unused within the class. Consider removing if no external caller uses `GasAnomalyDetector.GAS_COLUMNS`. |
| 2 | NOTE | `gas.py` | 9 | `timestamp` fields typed as `str` rather than `datetime`. Works correctly but reduces type safety. Acceptable for this sprint; consider upgrading in a future cleanup pass. |

---

## Verdict: PASS WITH NOTES

All 10 Sprint B changes are correct, secure, and consistent with project guardrails. Two non-blocking notes identified (unused class attribute, `str` vs `datetime` typing). No STOP WORK or WARN conditions triggered. Defense-in-depth validation (API validator + DB CHECK constraint) for fault types is well-designed. Savepoint and FOR UPDATE changes correctly improve database safety without over-engineering.

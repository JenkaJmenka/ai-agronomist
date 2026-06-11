# Code Review — Gap 2: Incident Memory System (pgvector)

- **Reviewer:** code-reviewer
- **Date:** 2026-06-11
- **Verdict:** **APPROVED** (PASS WITH NOTES — all notes are WARN-level, none blocking)

## Scope

| Area | Files |
|---|---|
| Models / types | `src/db/models.py` (`Incident`, `IncidentAction`), `src/db/embedding_type.py` |
| Service layer | `src/db/incident_memory.py`, `src/db/vector_search.py`, `src/db/incident_analytics.py` |
| Migrations | `alembic/versions/001_initial.py`, `alembic/versions/002_add_incident_memory_tables.py`, `alembic/env.py` |
| API | `src/api/routes/incidents.py`, `src/api/schemas/incidents.py` |
| Tests | `tests/db/test_incident_memory.py` (14 tests), `tests/api/test_incident_api.py` (9 tests) |

Companion spec written alongside this review: `docs/incident_memory_schema.md`.

## Independent verification (re-run by reviewer)

- `pytest tests/db/test_incident_memory.py tests/api/test_incident_api.py` →
  **23 passed** in ~1s (async SQLite, numpy cosine fallback path).
- Alembic chain via CLI on scratch SQLite: `upgrade head` then
  `downgrade base` — both exit 0. The previously reported "no alembic env.py"
  blocker is resolved (`alembic/env.py` now exists, wired to
  `src.db.models.Base.metadata`, with async-URL → sync-URL normalization).
- Coverage of Gap 2 modules under these two test files alone:
  `models.py` 100%, `incident_analytics.py` 97.96%, `schemas/incidents.py`
  97.44%, `routes/incidents.py` 96.67%, `incident_memory.py` 88.89%,
  `vector_search.py` 84.85%, `embedding_type.py` 76.92%. The uncovered lines
  are almost exclusively PostgreSQL-only branches (see W1).
- Secrets scan over all Gap 2 source files: clean.

---

## STOP-WORK guardrail checks

| Check | Result |
|---|---|
| Tight coupling LSTM-VAE ↔ API layer | **PASS** — DB layer imports nothing from `src/models/`; the embedding arrives as a plain `list[float]` through the API schema. Dependency flow is one-directional: `embedding_type ← models ← vector_search ← incident_analytics ← incident_memory ← routes`. |
| Secrets / API keys in code | **PASS** — none found (grep for password/secret/key/token over `src/db/` and the incident routes). |
| Model checkpoint without metadata | **N/A** — no checkpoints touched in Gap 2. |
| Test coverage < 80% | **PASS** — Gap 2 modules average well above 80% from the two new suites alone (project-wide gate is QA's 90% run; per-module figures above). |

## WARN-level guardrail checks

- **Hard-coded thresholds:** PASS. All tunables are named module constants
  (`SQLITE_CANDIDATE_LIMIT`, `COSINE_NORM_EPSILON`, `DEFAULT_SUGGESTION_NEIGHBORS`,
  `DEFAULT_MIN_ACTION_CONFIDENCE`, `DEFAULT_MIN_RECURRENCES`,
  `MAX_SIMILAR_RESULTS`, `IVFFLAT_LISTS`, `TOP_ROOT_CAUSES_LIMIT`); dimensions
  come from `src.config.LATENT_DIM` (except the deliberately frozen
  `EMBEDDING_DIM = 8` in migration 002, which is correct migration practice and
  documented in-file).
- **Error handling:** PASS. `ValueError` → 404 mapping in routes;
  dimension mismatches rejected at both the Pydantic boundary (body) and a
  dedicated 422 check (query params); SQLite candidate-cap overflow logs a
  warning rather than failing silently.
- **Duplication:** PASS. Filter construction is shared
  (`build_incident_filters`) between search and analytics; helpers reused in
  tests.
- **File size cap (300 lines):** PASS — largest file is `incident_memory.py`
  at 255 lines; analytics was split out specifically to respect the cap.

---

## Detailed findings

### Tenant isolation — PASS

- `build_incident_filters` unconditionally injects
  `Incident.tenant_id == tenant_id` as the first condition; every read path
  (similarity search on both backends, statistics, recurring issues) goes
  through it.
- Mutations (`resolve_incident`, `mark_anomaly`) fetch via
  `_get_incident(incident_id, tenant_id)` which filters on **both** id and
  tenant. Cross-tenant access raises `ValueError` → HTTP 404, which correctly
  does not disclose whether the incident exists.
- Tests cover isolation at both layers: DB
  (`test_find_similar_tenant_isolation`) and API
  (`test_resolve_cross_tenant_returns_404`).
- `incident_actions` has no `tenant_id` column, but is only reachable through a
  tenant-checked incident — acceptable (see W4).

### Dual-backend correctness — PASS

- `EmbeddingType` correctly switches dialect impl (pgvector `Vector(dim)` on
  `postgresql`, JSON elsewhere), enforces dimension on bind, and normalizes
  result values to `list[float]` on both backends (pgvector returns numpy
  arrays). `cache_ok = True` is set, avoiding SQLAlchemy type-cache warnings.
- `compute_cosine_distance` matches pgvector `<=>` semantics
  (`1 - cosine_similarity`) with an epsilon guard for zero vectors; verified by
  tests (identical → ≈0, opposite → ≈2.0, full ordering check).
- The PG path's `bindparam(..., type_=Vector(dim))` +
  `.op('<=>', return_type=Float)` construction is correct and necessary — a
  TypeDecorator does not proxy pgvector's comparator. Lazy imports keep
  `pgvector` out of the SQLite dependency path.
- SQLite fallback caps candidates at 5,000 and logs a warning when hit
  (tested via `test_sqlite_candidate_cap_warning` with a patched limit).
- Naive-datetime handling for SQLite (`_ensure_utc`) is in place and exercised
  by `test_resolve_incident_writes_actions_and_time_to_resolve`, which
  deliberately writes a naive `detected_at`.

### Migration chain 001 → 002 — PASS

- `002.down_revision = '001_initial'` matches 001's actual revision id.
- Verified end-to-end through the alembic CLI (upgrade + full downgrade) on
  scratch SQLite during this review — the chain is now CLI-runnable, closing
  the previous "no env.py" gap.
- PostgreSQL-only DDL (`CREATE EXTENSION vector`, ivfflat index) properly
  guarded by dialect checks in both `upgrade()` and `downgrade()`; downgrade
  drops objects in correct dependency order (actions before incidents) and
  intentionally leaves the shared extension installed.
- Migration columns/indexes match the ORM model definitions exactly
  (including the two composite indexes declared in `__table_args__`).

### Security — PASS

- No secrets, no raw SQL string interpolation (the only `op.execute` literal is
  the constant `CREATE EXTENSION` statement), all queries parameterized through
  the ORM, embeddings bound via `bindparam`.
- Input validated at the boundary: Pydantic v2 schemas with length/range
  constraints (`tenant_id min_length=1`, `anomaly_score ≥ 0`, k clamped 1–50,
  `min_confidence` 0–1, notes/root-cause max lengths matching column sizes);
  embedding dimension enforced in three layers (schema validator, query-param
  check, `EmbeddingType.process_bind_param`).
- Error responses don't leak internals: 404 detail is a generic
  "not found for tenant" message.

### Test quality — PASS

- Behavior-focused, independent tests with isolated in-memory async engines per
  test; no inter-test dependencies; descriptive names.
- Edge cases covered: opposite/orthogonal vectors, empty history warning,
  idempotent feedback tagging, cap overflow, dimension rejection, cross-tenant
  404, naive datetimes.
- API tests correctly assert the project's custom error envelope
  (`message` present, `detail` absent) per the custom HTTPException handler.
- Distance assertions use `pytest.approx` — consistent with the documented
  "never exact-compare" project rule.

---

## Findings (non-blocking)

| ID | Severity | Finding |
|---|---|---|
| W1 | WARN | **PostgreSQL/pgvector path has no automated test.** `_find_similar_pgvector`, `EmbeddingType.load_dialect_impl` (PG branch) and the ivfflat DDL are only exercisable against a live PostgreSQL instance; CI runs SQLite. The dev docker stack now ships pgvector — recommend a small opt-in integration test (e.g. marked `@pytest.mark.postgres`) before relying on the PG path in production. |
| W2 | WARN | **ivfflat index is created on an empty table** in migration 002. ivfflat centroids built over no data give poor recall; pgvector recommends building (or reindexing) after a representative data load. Recommend documenting a post-load `REINDEX` step in the deployment runbook (now noted in `docs/incident_memory_schema.md`). |
| W3 | INFO | **Schema drift in 001 vs models:** `001_initial` creates `sensor_readings.tenant_id`/`greenhouse_id` as `String(100)` while the ORM model declares `String(255)`. Pre-existing (not introduced by Gap 2), harmless on SQLite/PostgreSQL in practice, but worth a corrective migration whenever the next schema revision lands. |
| W4 | INFO | `incident_actions` carries no `tenant_id`; isolation is transitive via the FK. Safe under the current access patterns (actions are never queried directly by API), but if a future endpoint exposes actions directly it must join through `incidents` for the tenant check. |
| W5 | INFO | `resolve_incident` is not idempotent/guarded: re-resolving an already-resolved incident silently overwrites root cause and appends duplicate `IncidentAction` rows. Acceptable for now; consider a 409 or explicit re-open flow later. |
| W6 | INFO | `effectiveness_score`, `time_to_detect`, `reported_at`, `crop_damage_estimated`, `root_cause_confidence`/`root_cause_details` are schema-only — no code path writes them yet. Presumably reserved for Gap 3 (online learning); fine, but they are dead columns until then. |
| W7 | INFO | `GET /api/incidents/similar` passes the embedding as 8 repeated query params. Works (and validated), but URL-unfriendly if `LATENT_DIM` ever grows; a POST search endpoint would scale better. |

## Verdict

**APPROVED.** All STOP-WORK guardrails pass, tenant isolation is enforced
consistently at every read and write path, the dual-backend design is correctly
and cleanly implemented with honest documentation of its limits, the migration
chain runs in both directions through the CLI, and the new test suites are
high quality and green (23/23, independently re-run). The seven notes above are
improvements for subsequent phases, none of which block merging Gap 2.

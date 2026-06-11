# Incident Memory Schema (Gap 2)

Specification of the incident memory persistence layer: schema, dual vector
backend, query patterns, and indexing. Source of truth is the implementation in
`src/db/models.py`, `src/db/embedding_type.py`, `src/db/vector_search.py` and
migration `alembic/versions/002_add_incident_memory_tables.py`.

## Purpose

Store every detected anomaly together with its LSTM-VAE latent embedding so the
platform can answer "have we seen this before?" — semantic similarity search,
root-cause suggestion mined from resolved history, and tenant-level analytics.

---

## 1. Tables

### 1.1 `incidents`

One row per detected anomaly. Defined by the `Incident` model
(`src/db/models.py`) and created by migration `002`.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `String(36)` PK | no | UUID4 as string — portable across SQLite/PostgreSQL (no native UUID dependency) |
| `tenant_id` | `String(255)` | no | Multi-tenant owner; indexed; **every query must filter on it** |
| `greenhouse_id` | `String(255)` | no | Zone within a tenant; default `'default'` |
| `detected_at` | `DateTime(tz)` | no | When the anomaly was detected; indexed |
| `resolved_at` | `DateTime(tz)` | yes | Null = still open |
| `reported_at` | `DateTime(tz)` | yes | When an operator reported it |
| `sequence` | `JSON` | no | Raw sensor readings that triggered the anomaly, shape `[seq_len, input_dim]` (20×4) |
| `sequence_length` | `Integer` | no | Timesteps in `sequence` |
| `anomaly_score` | `Float` | no | LSTM-VAE reconstruction error |
| `embedding` | `EmbeddingType(LATENT_DIM)` | no | Latent vector (encoder `mu`), dim = 8; **pgvector `Vector(8)` on PostgreSQL, JSON on SQLite** |
| `embedding_uncertainty` | `Float` | no | Mean latent std, `mean(exp(0.5*logvar))`; higher = less certain |
| `root_cause` | `String(256)` | yes | Label, e.g. `boiler_failure`; indexed |
| `root_cause_confidence` | `Float` | yes | 0–1 |
| `root_cause_details` | `JSON` | yes | Structured diagnosis |
| `resolved` | `Boolean` | no | Default `false` |
| `actions_taken` | `JSON` | yes | List of action labels applied at resolution |
| `actions_effective` | `Boolean` | yes | Did the actions fix it? |
| `effectiveness_score` | `Float` | yes | 0–1 |
| `time_to_detect` | `Integer` | yes | Minutes, onset → detection |
| `time_to_resolve` | `Integer` | yes | Minutes, detection → resolution (computed by `resolve_incident`) |
| `crop_damage_estimated` | `Float` | yes | Estimated crop loss % |
| `tags` | `JSON` | yes | User tags; feedback tags `confirmed_anomaly` / `false_positive` are appended here |
| `notes` | `String(1024)` | yes | Operator notes |

### 1.2 `incident_actions`

One row per action taken while resolving an incident — the training signal for
action recommendation. Rows are written by `IncidentMemory.resolve_incident`.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `String(36)` PK | no | UUID4 string |
| `incident_id` | `String(36)` FK → `incidents.id` | no | Indexed |
| `action` | `String(256)` | no | e.g. `restart_heating` |
| `action_time` | `DateTime(tz)` | no | Server default `CURRENT_TIMESTAMP` |
| `effectiveness` | `Boolean` | yes | `true` = fixed, `false` = did not help, `null` = unknown |
| `notes` | `String(512)` | yes | |

Tenant scoping for actions is transitive through the FK: actions are only ever
reached via a tenant-checked incident.

---

## 2. Dual vector backend

User-approved architecture decision: the same model and service code runs on
two storage backends, selected at runtime from the engine dialect. Callers of
`IncidentMemory` never branch on backend.

| | PostgreSQL (production) | SQLite (dev default + tests) |
|---|---|---|
| Embedding column | native pgvector `Vector(8)` | `JSON` array of floats |
| Distance computation | in-database, `<=>` cosine-distance operator | in-Python, numpy over candidate rows |
| Index acceleration | `ivfflat` (`vector_cosine_ops`) | none — full scan of filtered candidates |
| Candidate bound | `LIMIT k` after ordered ANN scan | hard cap of 5,000 filtered rows (`SQLITE_CANDIDATE_LIMIT`), warning logged when hit |
| Extra dependency | `pgvector`, `asyncpg` (lazily imported only on the PG path) | none |

Mechanics:

- `EmbeddingType(dim)` (`src/db/embedding_type.py`) is a SQLAlchemy
  `TypeDecorator`: `load_dialect_impl` returns pgvector `Vector(dim)` on the
  `postgresql` dialect and `JSON` everywhere else. It enforces the dimension on
  write (`ValueError` on mismatch) and normalizes reads to `list[float]`
  (pgvector returns numpy arrays, JSON returns lists).
- `is_postgres_session` (`src/db/vector_search.py`) inspects
  `session.bind.dialect.name` and routes to `_find_similar_pgvector` or
  `_find_similar_python`.
- Both paths return identical `(Incident, cosine_distance)` pairs, most similar
  first. The Python fallback (`compute_cosine_distance`) is defined to match
  pgvector's `<=>` semantics: `1 - cosine_similarity`, with an epsilon guard
  that maps degenerate zero vectors to distance `1.0`.
- Implementation gotcha (documented for maintainers): because `EmbeddingType`
  is a TypeDecorator it does **not** proxy pgvector's `.cosine_distance()`
  comparator, so the PG path builds the expression manually via
  `bindparam(..., type_=Vector(dim))` and `.op('<=>', return_type=Float)`.

Distance range: cosine distance ∈ [0, 2]. 0 = identical direction, 1 =
orthogonal, 2 = opposite. Both backends are covered by tests asserting
identical-vector distance ≈ 0 and opposite-vector distance ≈ 2 (SQLite path
in CI; the PG path shares filters and operator semantics).

---

## 3. Query patterns

### 3.1 Tenant isolation (mandatory)

All read paths share `build_incident_filters` (`src/db/vector_search.py`),
which **always** starts with `Incident.tenant_id == tenant_id`; analytics
(`src/db/incident_analytics.py`) reuses the same builder. Single-row mutations
(`resolve_incident`, `mark_anomaly`) go through
`IncidentMemory._get_incident(incident_id, tenant_id)`, which filters on both
id and tenant and raises `ValueError` (mapped to HTTP 404 by the API layer) on
cross-tenant access — a foreign tenant cannot distinguish "not mine" from
"does not exist".

### 3.2 Cosine similarity search

`find_similar_incidents(session, embedding, tenant_id, ...)`:

PostgreSQL (single SQL statement, index-eligible):

```sql
SELECT incidents.*, embedding <=> :query_embedding AS distance
FROM incidents
WHERE tenant_id = :tenant_id            -- always
  AND greenhouse_id = :greenhouse_id    -- optional
  AND detected_at >= :cutoff            -- optional (max_age_days)
  AND resolved IS TRUE                  -- optional (resolved_only)
ORDER BY distance
LIMIT :k;
```

SQLite: same WHERE clause with `LIMIT 5000`, then numpy cosine distance and a
sort in Python, returning the top k.

### 3.3 Optional filters

| Filter | Meaning |
|---|---|
| `greenhouse_id` | Restrict to one zone |
| `max_age_days` | Only incidents with `detected_at >=` UTC-now minus N days |
| `resolved_only` | Only resolved incidents — used by `suggest_fixes` so recommendations are mined exclusively from confirmed outcomes |

### 3.4 Aggregations

`compute_statistics` and `compute_recurring_issues` run tenant-scoped
`COUNT`/`AVG`/`GROUP BY root_cause` queries (with `HAVING count >= min_occurrences`
for recurring issues). These never touch the embedding column and behave
identically on both backends.

---

## 4. Indexes

Created by migration `002` (both backends unless noted):

| Index | Columns | Purpose |
|---|---|---|
| `ix_incidents_tenant_id` | `tenant_id` | Baseline tenant scoping |
| `ix_incidents_detected_at` | `detected_at` | Age-window filters |
| `ix_incidents_root_cause` | `root_cause` | Root-cause grouping/analytics |
| `ix_incidents_tenant_detected` | `(tenant_id, detected_at)` | The dominant query shape: tenant + recency |
| `ix_incidents_resolved_detected` | `(resolved, detected_at)` | `resolved_only` + recency searches |
| `ix_incident_actions_incident_id` | `incident_id` | FK lookups |
| `ix_incidents_embedding_ivfflat` | `embedding` | **PostgreSQL only** — ANN cosine search |

### PostgreSQL: ivfflat

```sql
CREATE INDEX ix_incidents_embedding_ivfflat ON incidents
USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

- `lists = 100` follows pgvector guidance for datasets up to ~100K rows.
- ivfflat is **approximate**: recall depends on `probes` (session setting) and
  on the index having been built over representative data. Because migration
  `002` creates the index on an empty table, the cluster centroids are
  meaningless until data exists — plan a `REINDEX` (or drop/recreate) once a
  realistic volume of incidents has accumulated.
- The migration also runs `CREATE EXTENSION IF NOT EXISTS vector` (and the dev
  docker stack pre-enables it via `docker/initdb/01-enable-pgvector.sql`).
  Downgrade intentionally leaves the extension installed (shared resource).

### SQLite limitations

- No vector index of any kind: every similarity query scans all rows matching
  the metadata filters, capped at 5,000 candidates. Past the cap, results are
  potentially incomplete and a warning is logged.
- Exact (not approximate) within the cap — fine for dev/test datasets,
  unsuitable for production scale. Production must run PostgreSQL + pgvector.
- SQLite returns naive datetimes; the service layer normalizes them to UTC
  (`_ensure_utc`) before arithmetic such as `time_to_resolve`.

---

## 5. Migration chain

```
001_initial (sensor_readings)  →  002 (incidents, incident_actions, indexes, pgvector DDL)
```

- `002.down_revision = '001_initial'`. The chain runs cleanly through the
  alembic CLI in both directions (verified `upgrade head` → `downgrade base`
  on scratch SQLite, 2026-06-11).
- `002` freezes `EMBEDDING_DIM = 8` as a literal — migrations are immutable
  snapshots and deliberately do not import `src.config`. If `LATENT_DIM` ever
  changes, a new migration must alter the column and the ivfflat index.
- All PostgreSQL-only DDL (extension, ivfflat) is guarded by a dialect check,
  so the same migration file runs on SQLite.

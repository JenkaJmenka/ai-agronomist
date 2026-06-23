# BUG-011: PostgreSQL/pgvector path has no automated test

**Status:** Closed (2026-06-15)  
**Severity:** Medium (production risk on PG deploy)  
**Source:** [gap2_incident_memory_review.md](../review/gap2_incident_memory_review.md) (W1)

## Summary

`_find_similar_pgvector`, `EmbeddingType` PG branch, and ivfflat DDL are only exercisable against live PostgreSQL. CI runs SQLite only.

## Fix (implemented)

1. `tests/integration/test_pgvector_incident_memory.py` — `@pytest.mark.postgres` opt-in tests.
2. `.github/workflows/postgres-tests.yml` — CI job with `pgvector/pgvector:pg15`.
3. ivfflat `REINDEX` documented in [incident_memory_schema.md](../incident_memory_schema.md) and [DOCKER_SETUP.md](../DOCKER_SETUP.md).

## Acceptance criteria

- [x] PG similarity search tested in CI (docker job)
- [x] SQLite fallback still tested (`tests/db/test_incident_memory.py`)
- [x] Deployment runbook updated for ivfflat reindex after data load

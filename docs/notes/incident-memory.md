# Incident Memory — Notes

**Source:** gap2_incident_memory_review.md

## NOTE-IM-001: ivfflat index on empty table

Migration 002 creates ivfflat before data load. Poor recall until reindex. Document post-load `REINDEX` in deployment runbook.

**See also:** [BUG-011](../bugs/BUG-011-pgvector-path-untested.md)

---

## NOTE-IM-002: Schema drift tenant_id length

`001_initial` uses `String(100)` for tenant_id; ORM uses `String(255)`. Harmless in practice; fix in next migration.

---

## NOTE-IM-003: incident_actions has no tenant_id

Isolation transitive via FK. Safe now; future direct action queries must join through incidents.

---

## NOTE-IM-004: Dead schema columns

`effectiveness_score`, `time_to_detect`, `reported_at`, `crop_damage_estimated`, `root_cause_confidence`, `root_cause_details` — no code writes them yet. Reserved for future phases.

---

## NOTE-IM-005: GET /similar embedding as query params

8 repeated query params for LATENT_DIM=8. URL-unfriendly if dimension grows. Consider POST search endpoint.

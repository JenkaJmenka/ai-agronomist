-- BACKLOG-14 Phase 2 — read-only production report (user item 2).
--
-- For the user to run by hand against production Postgres. Nobody else
-- connects on this agent's behalf: this file was authored and verified
-- against the local SQLite test schema only (see
-- docs/handoff/backlog14_pass1_observability.md and
-- project_postgres_tests_are_ci_only). It has NOT been run against
-- production. Don't touch the rows it reports on — this is strictly
-- SELECT-only, wrapped in a read-only transaction so it cannot write even
-- by accident.
--
-- Schema facts this file depends on (verified against src/db/models.py):
--   * model_version's tenant column is named customer_id, not tenant_id.
--   * model_version.metrics_json is a String column (not JSON/JSONB), so it
--     must be cast with ::jsonb before any -> / ->> path operator works.
--   * Every row before this task (2026-11-08) was written with only the
--     four original keys (source, pass1_clean_rows, pass1_anomaly_rows,
--     calibration_threshold) -- the per-channel/discard/warning keys this
--     task adds do not exist on any row older than this change. Query 1
--     derives a discard fraction from whichever shape a given row has, and
--     returns NULL rather than erroring or silently dropping the row when
--     neither shape yields one.
--   * incidents has tenant_id, detected_at, anomaly_score directly -- no
--     cast needed there.
--
-- Known limitation, stated rather than silently assumed: query 1's
-- metrics_json -> jsonb cast is guarded only against NULL and
-- non-object-shaped strings (the `~ '^\s*\{'` check below), not against a
-- syntactically-invalid embedded token such as a bare NaN. Postgres 15 (this
-- project's pinned image, pgvector/pgvector:pg15 in docker-compose.yml) has
-- no built-in safe-cast / "is valid json" function -- that landed in
-- Postgres 16 as pg_input_is_valid() -- and a true try/catch needs a
-- PL/pgSQL function, which is DDL and cannot run inside BEGIN READ ONLY.
-- This is believed safe for every row written before this task: the four
-- original keys are two ints, a string and a percentile-derived float, and
-- the four stored tenant_1 calibration_threshold values on record
-- (0.0614-0.0650) are all finite. It is not proven safe for every row in
-- production, which this agent has not queried. If query 1 ever errors with
-- a JSON parse failure, that is itself a finding -- it means some historical
-- row already carries the hazard this task's allow_nan=False guard exists
-- to prevent, and it should be reported rather than worked around by
-- loosening this file.

BEGIN READ ONLY;

-- ---------------------------------------------------------------------
-- Query 1 — every model_version row since 2026-07-24 (commit 892bcd3,
-- which introduced the MAD scrub and so the regression), all tenants,
-- with the Pass-1 discard fraction derived per row.
-- ---------------------------------------------------------------------
WITH parsed AS (
    SELECT
        mv.*,
        CASE
            WHEN mv.metrics_json IS NOT NULL
                 AND mv.metrics_json ~ '^\s*\{'
            THEN mv.metrics_json::jsonb
            ELSE NULL
        END AS m
    FROM model_version AS mv
    WHERE mv.created_at >= TIMESTAMPTZ '2026-07-24 00:00:00+00'
)
SELECT
    id                                          AS model_id,
    customer_id                                 AS tenant_id,
    model_type,
    status,
    created_at,
    (m ->> 'calibration_threshold')::double precision   AS calibration_threshold,
    (m ->> 'pass1_clean_rows')::bigint                   AS pass1_clean_rows,
    (m ->> 'pass1_anomaly_rows')::bigint                 AS pass1_anomaly_rows,
    -- New-shape rows (this task) carry the discard guard's own fraction,
    -- already computed against the "rows carrying data" denominator -- use
    -- it directly when present. Older rows have no pass1_discard object at
    -- all, so fall back to anomaly / (clean + anomaly), which was the whole
    -- frame before BACKLOG-14 Phase 1 split "clean" and "anomaly" into
    -- anything narrower than the full resampled frame. NULL -- not 0 and
    -- not a dropped row -- when neither shape yields a denominator.
    COALESCE(
        (m -> 'pass1_discard' ->> 'data_discard_fraction')::double precision,
        CASE
            WHEN (m ->> 'pass1_clean_rows') IS NOT NULL
                 AND (m ->> 'pass1_anomaly_rows') IS NOT NULL
                 AND ((m ->> 'pass1_clean_rows')::bigint
                      + (m ->> 'pass1_anomaly_rows')::bigint) > 0
            THEN (m ->> 'pass1_anomaly_rows')::double precision
                 / ((m ->> 'pass1_clean_rows')::bigint
                    + (m ->> 'pass1_anomaly_rows')::bigint)
            ELSE NULL
        END
    ) AS pass1_discard_fraction,
    -- NULL on every row older than this task -- the key does not exist yet.
    m -> 'pass1_discard' ->> 'status'           AS pass1_discard_status
FROM parsed
ORDER BY created_at;

-- ---------------------------------------------------------------------
-- Query 2 — count of tenant_1 alerts raised under the 0.064 threshold.
--
-- "Alert" here means `incidents`, not `anomaly_episodes`: 0.064 sits in the
-- 0.0614-0.0650 band of tenant_1's four recorded cold-start
-- `calibration_threshold` values (AGENT_MEMORY.md, "Training is
-- non-deterministic"), which calibrates against LSTM-VAE reconstruction
-- error -- exactly what `incidents.anomaly_score` stores. `anomaly_episodes`
-- is per-channel detector state (stuck/band-breach/drift fault windows) with
-- no `anomaly_score` column and no relationship to this threshold, so it
-- cannot answer this question.
--
-- Reads as "how many recorded incidents would still count as an alert under
-- a 0.064 cut" -- i.e. anomaly_score >= 0.064 -- not "incidents whose score
-- happens to be below it".
-- ---------------------------------------------------------------------
SELECT
    COUNT(*) AS tenant_1_alerts_at_or_above_0_064
FROM incidents
WHERE tenant_id = 'tenant_1'
  AND anomaly_score >= 0.064;

COMMIT;

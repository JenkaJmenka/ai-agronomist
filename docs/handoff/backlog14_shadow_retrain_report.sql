-- BACKLOG-14 review round — read-only SQL for the shadow-retrain comparison
-- (docs/runbooks/cold-start-retraining.md, scripts/pass1_shadow_score.py).
--
-- For the user to run by hand against production Postgres, alongside the
-- shadow script. Agents have no production access, by the user's decision —
-- this agent has NOT executed this file and has not connected to Railway or
-- any Postgres instance. SELECT-only, wrapped in a read-only transaction so
-- it cannot write even by accident; no DDL.
--
-- Schema facts (same as docs/handoff/backlog14_pass1_metrics_report.sql):
--   * model_version's tenant column is customer_id, not tenant_id.
--   * model_version.metrics_json is a String column -- cast with ::jsonb.
--   * The typed per-reading table is sensor_readings (raw_readings stores a
--     JSON blob keyed by cleaned column name and is not used here).
--
-- Purpose of each query, matched to the runbook's step 2 hand-over list
-- ("both alert counts, both thresholds, the new metrics_json"): this file
-- supplies the OLD side of that comparison -- the currently active model's
-- own recorded metrics -- plus the data span needed to pick
-- --window-start/--window-end deliberately, which the runbook already warns
-- against hardcoding as "the last 30 days".
--
-- Assumption stated, not hardcoded: this file does NOT pick a window for you.
-- Query 2 reports tenant_1's actual sensor_readings span so the user (or the
-- agent preparing the shadow-score command, per the runbook's step 0 table)
-- chooses --window-start/--window-end from what production actually has.
-- Per project_future_dated_readings, this corpus is known to run ahead of
-- wall-clock in places, so "now minus 30 days" is specifically the wrong
-- default here, not just an arbitrary one -- a relative window could select
-- an empty range the same way the runbook describes for the local corpus.

BEGIN READ ONLY;

-- ---------------------------------------------------------------------
-- Query 1 — tenant_1's currently active model and its recorded metrics.
-- This is the "old" side of the shadow comparison: whatever the shadow
-- script reports for --old-checkpoint should be checked against what this
-- row actually has on file, not assumed to match.
-- ---------------------------------------------------------------------
SELECT
    mv.id                     AS model_id,
    mv.customer_id            AS tenant_id,
    mv.model_type,
    mv.checkpoint_path,
    mv.status,
    mv.created_at,
    mv.metrics_json::jsonb    AS metrics
FROM model_version AS mv
JOIN model_deployment AS md
    ON md.model_id = mv.id
WHERE md.customer_id = 'tenant_1'
  AND md.is_current = true;

-- ---------------------------------------------------------------------
-- Query 2 — the actual span and volume of tenant_1's sensor_readings, so
-- the comparison window is chosen against what production has, not assumed.
-- ---------------------------------------------------------------------
SELECT
    MIN(timestamp) AS earliest_reading,
    MAX(timestamp) AS latest_reading,
    COUNT(*)        AS total_rows
FROM sensor_readings
WHERE tenant_id = 'tenant_1';

-- ---------------------------------------------------------------------
-- Query 3 — monthly row counts over that span, so a window can be picked
-- for density (a window landing mostly on a sparse month would understate
-- how many alerts either model raises) as well as recency.
-- ---------------------------------------------------------------------
SELECT
    date_trunc('month', timestamp) AS month,
    COUNT(*)                        AS rows_in_month
FROM sensor_readings
WHERE tenant_id = 'tenant_1'
GROUP BY 1
ORDER BY 1;

COMMIT;

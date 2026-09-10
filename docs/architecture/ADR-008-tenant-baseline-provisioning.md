# ADR-008: Per-Tenant Ensemble Baseline Provisioning

**Date:** 2026-09-10
**Status:** Accepted
**Deciders:** Orchestrator, Architect

## Context

`EnsembleAnomalyDetector` is always fitted against
`build_default_baseline()` — **10 synthetic sequences** generated from
sensor-range midpoints (`src/api/services/comprehensive_pipeline.py:362`).
`_prepare_baseline` reads this default when no tenant-specific baseline
is provided (`comprehensive_pipeline.py:499–506`), and `run_batch` calls
`_prepare_baseline(None)` (`comprehensive_pipeline.py:588`), so on the
batch path every tenant is scored against the same synthetic notion of
"normal".

For anomaly detection that is close to meaningless. The Mahalanobis
distance, IsolationForest scores, and statistical z-scores are all
computed relative to a covariance / distribution that has never seen
real sensor data. This is a plausible contributor to the measured
Mahalanobis inversion: the detector fires 38.3% of the time on healthy
clear-sky data vs 9.5% on the real stuck fault (measured 2026-09-10).
When "normal" is fiction, deviations from it are noise, not signal.

### Infrastructure that already exists

The infrastructure to fix this is present and unused:

- **`model_baseline` table** (`src/db/models.py:156`): `id`,
  `customer_id`, `greenhouse_type`, `baseline_json`, `created_at`.
  The table is defined and the model is imported by
  `src/models/model_registry.py:6`, but nothing reads or writes it.

- **The cold-start bootstrap already produces the right input.** The
  bootstrap pipeline outputs per-tenant physics-filtered clean rows at
  `data/bootstrapped_clean_{tenant_id}.csv`
  (`src/api/routes/bootstrap.py:95`, path template at
  `src/config.py:890`). As of the current phase the bootstrap retains
  100% of rows instead of 8.6%, so for the first time its output is
  trustworthy as a representation of the tenant's normal operating
  conditions.

## Decision

1. **Derive the per-tenant ensemble baseline from the cold-start
   bootstrap's clean output.** After the bootstrap pipeline writes
   `bootstrapped_clean_{tenant_id}.csv`, convert its rows to sequences
   (same windowing as the upload path: `SEQ_LEN=20`, stride 1) and
   persist the resulting array as a `ModelBaseline` row keyed by
   `customer_id`, versioned by `created_at`.

2. **`_prepare_baseline` reads the stored baseline when present and
   falls back to the synthetic default when absent.** Tenants without a
   bootstrap (or before their first upload) continue to use
   `build_default_baseline()`. This preserves backward compatibility —
   no tenant regresses.

3. **Re-derive the baseline on retrain.** When the LSTM-VAE is
   retrained for a tenant, the bootstrap clean output may have changed
   (new data, updated physics filters). The baseline row should be
   overwritten with a fresh derivation. Version the row by
   `created_at` so the previous baseline is recoverable via timestamp
   comparison if needed.

4. **The baseline must be fixed BEFORE recalibrating any
   ensemble-derived detector.** Recalibrating NoiseDetector or
   MultivariateMahalanobisDetector against a 10-synthetic-sequence
   baseline would tune detectors to a fictional normal, and the work
   would have to be redone once a real baseline is available. This is
   the sequencing constraint that motivated recording this decision
   now, before the detector recalibration work begins.

## Alternatives Considered

### Fit on the uploaded file itself

Rejected. The baseline would absorb the very fault being hunted. The
December fixture is 163.83 h frozen at exactly 100.0% RH — fitting a
baseline on it would make a dead sensor "normal". The baseline must
come from known-clean data, not from the file under analysis.

### Baselines keyed by `greenhouse_type`

Rejected. The `greenhouse_type` column in `model_baseline` invites
this, but tomato-vs-cucumber is far less predictive than
this-greenhouse-vs-itself. A greenhouse in Almería and one in Utrecht
growing the same crop have different temperature profiles, humidity
ranges, and CO2 patterns. Per-tenant baselines capture the specific
operating envelope; per-type baselines average it away.

### Keep the synthetic default

Rejected. The cheapest option, but leaves every ensemble-derived signal
fitted to fiction. The measured Mahalanobis inversion (38.3% on healthy
vs 9.5% on faulted) is evidence that the synthetic baseline actively
degrades detection quality. The 10 midpoint sequences have zero
variance within each sequence and zero inter-sequence diversity — every
real greenhouse deviates from them, so the detector fires on normalcy.

## Consequences

### Positive

- **Ensemble detectors score against reality.** Per-tenant baselines
  mean the IsolationForest, statistical Mahalanobis, and correlation
  components learn what *this greenhouse* looks like when healthy.
  Anomalies become deviations from the tenant's own operating envelope,
  not from a synthetic midpoint.
- **No schema change required.** The `model_baseline` table already
  exists with the right columns. The work is wiring, not migration.
- **Graceful degradation.** Tenants without a baseline continue to use
  the synthetic default. No existing tenant breaks.

### Negative / Risks

- **Bootstrap output quality is the new single point of trust.** If the
  physics filters in the bootstrap pass through faulty data, the
  baseline absorbs it. The bootstrap's 100% retention rate (up from
  8.6%) makes this more likely, not less. The baseline derivation
  should log the input row count and basic statistics (mean, std per
  sensor) for auditability.
- **Storage cost.** A baseline of 1,000 sequences × 20 steps × 4
  sensors × 4 bytes = ~320 KB per tenant in `baseline_json`. For tens
  of tenants this is negligible; for thousands it may warrant a more
  compact representation (e.g., store only the covariance matrix and
  mean, not the raw sequences). This is a future concern, not a
  blocking one.
- **Sequencing dependency.** The baseline must be provisioned before
  any ensemble-derived detector is recalibrated. This blocks the
  NoiseDetector and MultivariateMahalanobisDetector recalibration work
  identified in the convergence doc. The dependency is real and
  intentional — recalibrating against fiction wastes effort.

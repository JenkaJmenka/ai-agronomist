# ADR-004: Deterministic Attribution via decode(mu)

**Date:** 2026-06-16
**Status:** Accepted
**Deciders:** ML Engineer, Tech Lead

## Context

The LSTM-VAE uses a reparameterization trick during forward passes: the latent
vector is sampled as `z = mu + eps * std` where `eps ~ N(0,1)`. This means two
forward passes on the same input produce slightly different reconstruction
errors (~1e-3 variance), making per-sensor attribution scores non-reproducible
across calls.

`AnomalyAttributor` uses permutation-based SHAP approximation: it masks each
sensor channel with its baseline mean and measures how much reconstruction
error changes. Reproducible per-sensor deltas require a deterministic
reconstruction path.

## Decision

`AnomalyAttributor._reconstruction_error()` calls `model.encode()` to obtain
`mu` directly, then calls `model.decode(mu)` — bypassing the reparameterization
sampling in `model.forward()`. This gives a deterministic reconstruction path:
same input always produces the same attribution.

## Consequences

### Positive
- Attribution scores are fully reproducible across calls, enabling stable
  dashboards and regression tests with exact numeric assertions.
- Per-sensor contribution sums to 1.0 are preserved across requests.

### Negative / Constraints

**Threshold non-transferability:** Anomaly detection thresholds (e.g.
`ANOMALY_THRESHOLD = 0.8`) are calibrated on the stochastic `forward()` path.
Attribution scores produced by `decode(mu)` are numerically lower than
stochastic-path scores on the same input — the deterministic path removes
sampling noise. Do NOT use `ANOMALY_THRESHOLD` to interpret raw attribution
confidence values.

**`model.eval()` does not disable sampling:** `torch.no_grad()` and
`model.eval()` suppress dropout and batch-norm variance but do NOT disable the
VAE reparameterization (which is intentional during inference for uncertainty
estimation). The deterministic path via `decode(mu)` is the only reliable fix.

**API contract:** `AnomalyAttributor.attribute_sequence()` returns
`anomaly_error` and `baseline_error` computed via the deterministic path.
These values are NOT comparable to the `anomaly_score` field returned by
`/api/inference/comprehensive` (which uses `predict_batch_enriched()` via the
stochastic path). Downstream consumers must not mix these two score families.

## Alternatives Considered

1. **Average over N stochastic samples** — more accurate attribution but
   O(N) model calls per sensor channel per attribution request; too slow.
2. **Disable reparameterization with a flag** — would require modifying
   LSTMVAE architecture; violates single-responsibility and risks breaking
   training.
3. **Use `torch.manual_seed`** — makes tests reproducible but hides the
   non-determinism in production; rejected.

## Implementation Notes

- `AnomalyAttributor._reconstruction_error()` calls `model.eval()` at entry
  as a guard, then uses `model.encode(x)` → `(mu, _)` → `model.decode(mu)`.
- `AnomalyAttributor.set_baseline()` pre-computes `baseline_error` using the
  same deterministic path so both values are on the same scale.
- `AttributionStrength` class (now in `src/models/attribution_strength.py`)
  operates on normalized contributions and is unaffected by this choice.

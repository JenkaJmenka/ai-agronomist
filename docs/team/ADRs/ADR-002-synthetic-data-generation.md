# ADR-002: Synthetic Data Generation for Cold-Start Training

## Status

ACCEPTED

## Context

Phase 1 MVP must train anomaly detector before accessing real customer data.
Traditional SCADA blindness means we can't test on real greenhouse infrastructure failures.

Must generate realistic normal behavior to establish baseline.

## Problem

How to generate training data when real greenhouse data unavailable?

Options:

1. **Synthetic data** (chosen): TimeGAN-inspired generator
2. **Public datasets**: Limited, different crops/climates
3. **Simulation**: Custom greenhouse simulator (expensive)
4. **Hybrid**: Synthetic + limited real data (Phase 1.5)

## Decision

Build **TimeGAN-inspired synthetic data generator**:

- Temperature: 20-30°C, realistic gradients
- Humidity: 50-80%, correlated with temperature
- CO₂: 300-500 ppm, slow drift
- Light: 0-1000 lux day/night cycle
- 1000 sequences, 20 timesteps
- Reproducible (fixed random seed)

## Rationale

1. **Sufficient**: 1000 sequences adequate for VAE training
2. **Realistic**: Correlations and constraints enforced
3. **Reproducible**: Same seed = same data (important for research)
4. **Fast**: Generated on-the-fly, no storage needed
5. **Tunable**: Easy to adjust ranges for different climates

## Consequences

- Not identical to real greenhouse data (unavoidable at Phase 1)
- Model may need tuning when customer data arrives (Phase 2: transfer learning)
- Synthetic validation only; real validation delayed to Phase 2
- Correlations hardcoded; real data may have different patterns

## Alternatives Reconsidered

- Real data unavailable until Phase 2 (blocking constraint)
- Simpler random generation would lose correlations
- More complex simulation deferred to Phase 2 if needed

# ADR-001: LSTM-VAE Architecture for Anomaly Detection

## Status
ACCEPTED

## Context
Phase 1 MVP needs to detect greenhouse anomalies (equipment failures, parameter drift) 
invisible to traditional SCADA. We must train on normal behavior to detect deviations.

Key requirement: Unsupervised learning (no anomaly labels).

## Problem
What architecture best detects infrastructure failures in time-series sensor data?

Options considered:
1. **LSTM-Autoencoder**: Simple, fast, proven for time-series
2. **LSTM-VAE** (chosen): Probabilistic, better outlier detection, learns distribution
3. **GAN**: Harder to train, mode collapse risk
4. **Isolation Forest**: Shallow, limited temporal understanding
5. **Hybrid ensemble**: More complex, future option

## Decision
Use **LSTM-VAE** with 2-layer encoder/decoder, 8D latent space.

Architecture:
- Input: (batch, 20 timesteps, 4 sensors)
- Encoder LSTM: 64 → 32 hidden dims
- Latent: 8-dimensional distribution
- Decoder LSTM: Mirror of encoder
- Loss: Reconstruction (MSE) + KL divergence (beta=0.1)

## Rationale
1. **Unsupervised**: Learns normal behavior without labels
2. **Probabilistic**: VAE gives uncertainty estimates
3. **Temporal**: LSTM captures temporal patterns
4. **Interpretable**: Reconstruction error is anomaly signal
5. **Proven**: VAEs proven for time-series anomaly detection in industry
6. **Scalable**: 8D latent space is interpretable but flexible

## Consequences
- Training required on normal data (Phase 1: synthetic, Phase 2: customer data)
- Inference latency: ~10ms per sequence (acceptable)
- Requires GPU for real-time multi-customer deployment (Phase 2)
- Better anomaly detection but higher complexity than simple autoencoder

## Alternatives Reconsidered
- Isolation Forest added as secondary anomaly detector (Phase 2)
- LSTMs for direct forecasting (alternative, deferred)

# ADR-002: Synthetic Data Generation for Model Pre-Training

**Date**: 2026-06-09
**Status**: Proposed

## Context

The LSTM-VAE model requires a large and diverse dataset for effective training. In the early stages of the project, real-world data may be scarce or not representative of all possible scenarios. To address this, we need a strategy for generating synthetic data that can be used to pre-train the model and ensure its robustness.

## Decision

We will implement a synthetic data generator (`src/models/synthetic_data.py`) with the following capabilities:

1.  **Baseline Generation**: Generate "normal" sensor readings based on configurable sinusoidal patterns, representing typical diurnal and seasonal cycles.
2.  **Anomaly Injection**: Introduce various types of anomalies into the baseline data, including spikes, drops, and flatlines, to simulate equipment malfunctions or other unusual events.
3.  **Configurability**: The data generator will be highly configurable, allowing us to control the characteristics of the generated data (e.g., noise levels, anomaly frequency, and magnitude).

## Consequences

**Pros**:
- Enables model training before sufficient real-world data is available.
- Improves model robustness by exposing it to a wide range of normal and anomalous conditions.
- Facilitates testing and validation of the anomaly detection system.

**Cons**:
- Synthetic data may not perfectly capture the complexities and nuances of real-world data, potentially leading to a "sim-to-real" gap.
- The quality of the synthetic data is highly dependent on the accuracy of the underlying generation models.

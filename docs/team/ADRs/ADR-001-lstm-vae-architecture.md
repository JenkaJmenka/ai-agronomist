# ADR-001: LSTM-VAE Architecture for Anomaly Detection

- Status: Proposed
- Deciders: Architect
- Date: 2026-06-08

## Context and Problem Statement

The AI Agronomist platform requires a robust anomaly detection system to identify unusual patterns in sensor data from greenhouses. These anomalies could indicate equipment malfunction, disease outbreaks, or other critical events. We need a model that can learn the normal temporal patterns in multi-variate sensor data and flag deviations.

## Decision Drivers

- **Temporal Dependencies:** Sensor data is time-series data, so the model must capture temporal relationships.
- **Unsupervised Learning:** We do not have a labeled dataset of anomalies, so the model must be unsupervised.
- **Multi-variate Data:** The model needs to handle multiple sensor streams simultaneously (e.g., temperature, humidity, CO2, light).
- **Interpretability:** While not a primary driver, some level of insight into why a point is anomalous is desirable.

## Considered Options

1.  **Isolation Forest:** A tree-based anomaly detection algorithm. Simple and fast, but does not explicitly model temporal dependencies.
2.  **One-Class SVM:** A kernel-based method for novelty detection. Can be effective but struggles with high-dimensional time-series data.
3.  **LSTM Autoencoder:** A neural network architecture that uses LSTMs to learn a compressed representation of sequential data. Good for temporal data, but can be improved.
4.  **LSTM-based Variational Autoencoder (LSTM-VAE):** An extension of the LSTM Autoencoder that models the data distribution in the latent space. This allows for probabilistic anomaly detection and can be more robust to noise.

## Decision Outcome

Chosen option: **LSTM-based Variational Autoencoder (LSTM-VAE)**, because it is well-suited for learning the complex temporal dynamics of multi-variate sensor data in an unsupervised manner. The probabilistic nature of the VAE allows for a more nuanced approach to anomaly detection.

### Positive Consequences

- The model can capture non-linear temporal dependencies in the data.
- It provides a probabilistic measure of anomaly, which can be thresholded to control the sensitivity of the detector.
- The learned latent space can potentially be used for other tasks, such as data visualization or root cause analysis.

### Negative Consequences

- VAEs are more complex to implement and train than simpler models like Isolation Forest.
- The training process can be sensitive to hyperparameter choices.
- Inference time may be higher than for non-neural network models.

## Architecture

### Input

- **Shape:** `(batch_size, sequence_length, num_features)`
- **`sequence_length`:** 20 (representing 20 time steps of sensor data)
- **`num_features`:** 4 (e.g., temperature, humidity, CO2, light intensity)

### Encoder

- **LSTM Layer 1:** 64 hidden units
- **LSTM Layer 2:** 32 hidden units
- The final hidden state of the second LSTM is passed to two separate dense layers to produce the mean (`mu`) and log-variance (`log_var`) of the latent space distribution.

### Latent Space

- **Dimension:** 8
- **Reparameterization Trick:** `z = mu + std * epsilon`, where `std = exp(0.5 * log_var)` and `epsilon` is sampled from a standard normal distribution.

### Decoder

- **LSTM Layer 1:** 32 hidden units
- **LSTM Layer 2:** 64 hidden units
- A final dense layer with a linear activation function reconstructs the input sequence.

### Loss Function

- **Reconstruction Loss:** Mean Squared Error (MSE) between the input and reconstructed sequences.
- **KL Divergence Loss:** The Kullback-Leibler divergence between the learned latent distribution and a standard normal distribution. This acts as a regularization term.
- **Total Loss:** `reconstruction_loss + beta * kl_loss`
- **`beta`:** 0.1

## Validation

The model's performance will be validated by its ability to reconstruct normal sensor data with low error while producing high reconstruction errors for known or synthetic anomalies. The distribution of reconstruction errors on a hold-out set of normal data will be used to set an anomaly threshold.

## ADR-001-lstm-vae-architecture.md

The LSTM-VAE architecture will be documented in `docs/architecture/lstm_vae_design.md`.

## Links

- [Original Task](https://[link-to-task-tracker]/task/123)
- [VAE Paper](https://arxiv.org/abs/1312.6114)

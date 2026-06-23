# Greenhouse Anomaly Detection Models

This directory contains the ML foundation for the AI Agronomist platform. The core architecture relies on an **LSTM-VAE** to learn the normal behavior of greenhouse sensor data and detect anomalies.

## Core Components

### `lstm_vae.py`
The primary anomaly detection model combining LSTMs for time-series modeling with a Variational Autoencoder (VAE) for robust representation learning.
- **Encoder**: 2 LSTM layers mapping sequence to latent parameters (`mu`, `logvar`).
- **Latent Space**: Continuous normal distribution representing normal operation.
- **Decoder**: 2 LSTM layers mapping latent samples back to sensor sequences.
- **Loss**: Reconstruction (MSE) + `beta` * KL Divergence.

### `data_generator.py`
Synthetic data generator mimicking normal greenhouse operations to train the model.
- Includes physical constraints, temporal correlations, and day/night patterns.
- Output shapes: `(batch_size, seq_len=20, input_dim=4)`.

### `data_utils.py`
Provides pre-processing, dataloader creation, and normalization scaling logic.

## Data Schema (Sensors)
| Feature | Description | Range |
|---------|-------------|-------|
| 1 | Temperature | 20-30°C |
| 2 | Humidity | 50-80% |
| 3 | CO2 | 300-500 ppm |
| 4 | Light | 0-1000 lux |

For configuration details like hyperparameters, refer to `src/config.py`.

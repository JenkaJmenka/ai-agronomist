# ML Guardrails

## LSTM-VAE spec (locked — do not change without ADR)
LSTMVAE(input_dim=4, hidden_dims=[64,32], latent_dim=8, seq_len=20, beta=0.1, dropout=0.2)
Loss: MSE(x, x_recon) + beta * KL(N(μ,σ²) || N(0,I))
Latent: z = μ + σ ⊙ ε, ε ~ N(0,I)

## Sensor ranges (physical units)
- Temperature: 20–30°C
- Humidity: 50–80%
- CO2: 300–500 ppm
- Light: 0–1000 lux

## STOP WORK
- Checkpoint without: epoch, loss, accuracy, timestamp, model_config
- NaN/Inf in model outputs with no detection
- Training loop with no early stopping
- Hard-coded sensor ranges (must reference src/config.py)

## WARN
- Beta > 1.0 (risk of posterior collapse)
- Dropout > 0.5 (too aggressive)
- Learning rate > 1e-2 (unstable)

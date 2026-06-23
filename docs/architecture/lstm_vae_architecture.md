# LSTM-VAE Architecture: Greenhouse Anomaly Detection

## 1. Overview
This document defines the architecture for the LSTM-Variational Autoencoder (LSTM-VAE) designed to detect infrastructure failures in greenhouses (e.g., boiler degradation, power quality issues) by identifying anomalies in multi-dimensional sensor time-series data.

## 2. Data Model

### 2.1 Input Tensor Structure
The model processes a 3D tensor representing a sliding window of sensor readings.

| Dimension | Name | Size | Description |
|-----------|------|------|-------------|
| 0 | `batch_size` | Variable | Number of sequences in a batch |
| 1 | `seq_len` | 20 | Temporal window size (look-back) |
| 2 | `input_dim` | 4 | Number of sensor features |

### 2.2 Sensor Feature Schema
Each time step $t$ in the sequence contains the following 4 features:

| Feature | Unit | Range | Description |
|---------|------|-------|-------------|
| `temperature` | °C | 2-35 | Ambient air temperature |
| `humidity` | % | 40-90 | Relative humidity |
| `CO2` | ppm | 300-1000 | Carbon dioxide concentration |
| `light` | lux | 0-2000 | Ambient light intensity |

### 2.3 Latent Space Representation
The bottleneck layer represents the compressed state of the greenhouse environment.

- **Latent Dimension (`latent_dim`)**: 8
- **Representation**: A vector $z \sim q_\phi(z|x)$ parameterized by $\mu$ (mean) and $\log \sigma^2$ (log variance).
- **Encoding Process**: An LSTM encoder processes the input sequence $x$, mapping the temporal dependencies to a fixed-size hidden state, which is then projected to the $\mu$ and $\log \sigma^2$ vectors.
- **Decoding Process**: The decoder takes a sampled $z$ from the latent distribution and reconstructs the full sequence $\hat{x}$ using an LSTM decoder.

### 2.4 Output Tensor Structure
The decoder reconstructs the input sequence, producing a tensor of shape `[batch_size, 20, 4]`.

## 3. Key Design Decisions

### 3.1 Architecture Choice: LSTM-VAE
The LSTM-VAE architecture was selected because greenhouse anomalies (like boiler failure) manifest as temporal drifts in sensor readings. Standard VAEs struggle with time-series dependencies, whereas the LSTM layers allow the model to learn the underlying temporal dynamics and capture "normal" environmental patterns over time.

### 3.2 Sequence Length Justification (`seq_len=20`)
A sequence length of 20 steps was chosen based on a 2-hour sampling interval. This provides a 40-hour window, which is sufficient to capture:
- Diurnal (day/night) temperature and light cycles.
- The 24-48 hour lead time required for infrastructure failure detection (e.g., detecting a slow drop in $CO_2$ or rising temperature trends before plant damage occurs).

### 3.3 $\beta$-VAE Loss Formulation
To improve the quality of the latent space, we use the $\beta$-VAE objective:

$$L(\theta, \phi; x, z) = \mathbb{E}_{q_\phi(z|x)}[\log p_\theta(x|z)] - \beta \cdot D_{KL}(q_\phi(z|x) || p(z))$$

- **Reconstruction Term**: Mean Squared Error (MSE) between input and reconstructed sensor values.
- **KL Divergence Term**: Regularizes the latent distribution towards $\mathcal{N}(0, I)$.
- **$\beta$ Hyperparameter**: We set $\beta > 1$ to encourage **disentanglement**. This forces the model to learn independent latent factors (e.g., one factor for diurnal cycle, one for humidity shifts), making anomalies (which deviate from these factors) easier to identify.

### 3.4 Latent Dimension Bottleneck (`latent_dim=8`)
The 8-dimensional bottleneck is a strategic compromise:
- **Small enough** to act as a meaningful information bottleneck, forcing the model to discard high-frequency sensor noise.
- **Large enough** to encode the complex inter-dependencies between the 4 sensors and the temporal features across 20 time steps.

## 4. Component Interaction

### 4.1 Data Generation & Loading
1.  **`data_generator.py`**: Generates synthetic sensor time-series data based on defined ranges (temperature, humidity, etc.) and injects anomalies (e.g., sudden spikes, drifts).
2.  **`DataLoader` (PyTorch)**: A `Dataset` implementation that reads from the generator, applies windowing (sliding window of 20 steps), and produces tensors of shape `[batch_size, 20, 4]`.

### 4.2 Model Execution Flow
1.  **Encoder**: Receives input $x \in \mathbb{R}^{B \times 20 \times 4}$, processes through LSTM layers, and outputs $\mu$ and $\log \sigma^2$.
2.  **Reparameterization**: Samples $z = \mu + \epsilon \cdot \exp(0.5 \cdot \log \sigma^2)$ where $\epsilon \sim \mathcal{N}(0, I)$.
3.  **Decoder**: Takes $z$ and reconstructs the sequence $\hat{x} \in \mathbb{R}^{B \times 20 \times 4}$.
4.  **Anomaly Detection**: During inference, the reconstruction error (MSE) between $x$ and $\hat{x}$ is calculated. If $MSE > \text{threshold}$, an anomaly is flagged.

## 5. Implementation TODOs

- [ ] **ml-engineer**: Implement `src/models/lstm_vae.py` with the specified LSTM and Dense topology.
- [ ] **ml-engineer**: Implement the $\beta$-DSS loss function and the reparameterization trick.
- [ ] **ml-engineer**: Develop the `data_generator.py` to produce training tensors in the `[batch, 20, 4]` format.
- [ ] **backend-engineer**: Build the FastAPI inference endpoint to accept sensor streams and return anomaly scores.
- [ ] **qa-engineer**: Implement integration tests verifying that the reconstruction error increases when injecting synthetic anomalies (e.g., sudden temperature spikes).

## 6. File Ownership

| Agent | Responsible Files |
|-------|-------------------|
| `ml-engineer` | `src/models/lstm_vae.py`, `src/models/data_generator.py` |
| `backend-engineer` | `src/api/inference.py`, `src/api/routes.py` |
| `qa-engineer` | `tests/test_vae.py`, `tests/test_api.py` |
| `devops-lead` | `docker/Dockerfile`, `infra/deployment.yaml` |

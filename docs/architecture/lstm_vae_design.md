# LSTM-VAE Architecture Design: Greenhouse Anomaly Detection

## 1. Architecture Overview
The system utilizes a Long Short-Term Memory Variational Autoencoder (LSTM-VAE) to detect infrastructure anomalies in greenhouse environments. The architecture is designed to capture temporal dependencies in multi-dimensional sensor time-series data.

### Core Components
- **Encoder**: A 2-layer LSTM network that compresses the input sequence into a latent representation.
- **Latent Space**: An 8-dimensional bottleneck layer representing the compressed state of the greenhouse environment.
- **Decoder**: A 2-layer LSTM network that reconstructs the input sequence from the sampled latent vector.

### Model Specification
The model is instantiated with the following fixed hyperparameters:
`LSTMVAE(input_dim=4, hidden_dims=[64,32], latent_dim=8, seq_len=20, beta=0.1, dropout=0.2)`

## 2. Loss Function
The training objective is a $\beta$-VAE loss function, which balances reconstruction accuracy with latent space regularization.

### Components
- **Reconstruction Loss**: Mean Squared Error (MSE) calculated between the original input $x$ and the reconstructed sequence $\hat{x}$, mean-reduced over the batch.
- **KL Divergence**: The Kullback-Leibler divergence between the approximate posterior $q_\phi(z|x)$ and the prior $p(z) \sim \mathcal{N}(0, I)$, calculated as:
  $$KL = -0.5 \cdot \sum (1 + \log \sigma^2 - \mu^2 - \exp(\log \sigma^2)) / \text{batch\_size}$$
- **Total Loss**: 
  $$\text{Loss} = \text{MSE}(x, \hat{x}) + \text\_beta \cdot KL$$

### $\beta$ Hyperparameter Justification
We use $\beta = 0.1$. A value of $\beta < 1$ is chosen to provide **light regularization**. This prioritizes reconstruction quality (minimizing MSE) to ensure the model accurately captures the complex, high-fidelity sensor patterns necessary for precise anomaly detection, while still providing enough regularization to prevent the latent space from becoming unconstrained.

## 3. Hyperparameter Justification

| Hyperparameter | Value | Justification |
|:---|:---|:---|
| `input_dim` | 4 | Represents the core sensor suite: temperature, humidity, $\text{CO}_2$, and light. |
| `hidden_dims` | [64, 32] | Progressive compression: 64 units capture complex temporal patterns; 32 units force higher-level abstraction. |
| `latent_dim` | 8 | Provides sufficient capacity for 4-sensor inter-dependencies while forcing an information bottleneck. |
| `seq_len` | 20 | Represents a ~20-minute rolling window (assuming 1 reading/min), capturing short-term temporal dynamics. |
| `beta` | 0.1 | Balances reconstruction fidelity against latent space regularization. |
| `dropout` | 0.2 | Regularization technique to prevent overfitting on synthetic training datasets. |

## 4. Data Assumptions

### Sensor Ranges and Normalization
All input features are normalized to the range $[0, 1]$ based on fixed physical bounds:
- **Temperature**: 20–30°C
- **Humidity**: 50–80%
- **$\text{CO}_2$**: 300–500 ppm
- **Light**: 0–1000 lux

### Preprocessing
- **Temporal Correlation**: A 3-step smoothing kernel is applied to raw sensor streams to reduce high-frequency noise and stabilize the input for the LSTM layers.
- **Input Format**: Data is fed as a 3D tensor of shape `[batch_size, 20, 4]`.

## 5. Validation Approach

### Anomaly Scoring
- **Reconstruction Error**: The primary anomaly score is the MSE between the input and the reconstruction. An anomaly is flagged if the error exceeds a predefined threshold.
- **Latent Space Density**: We monitor the density of the latent space; points falling into low-density regions (out-of-distribution) are flagged as potential anomalies.

### Testing Strategy
- **Dataset Split**: Evaluation is performed on separate "Normal" and "Anomaly" test sets to measure precision and recall.
- **Metric**: Focus on the ability to detect known failure modes (e.g., sudden $\text{CO}_2$ drops) without high false-positive rates.

## 6. Phase 1.5 Extensibility

The architecture is designed with modularity in mind to support future evolution:
- **Pluggable Encoder/Decoder**: The LSTM architecture can be swapped for Transformer-based encoders (e.g., Informer or Autoformer) without changing the training pipeline.
- **Multi-tenant Model Registry**: Hooks are provided to allow the backend to load tenant-specific weights or fine-tuned versions of the base model.
- **Online Learning**: The design supports an incremental fine-tuning path, allowing the model to adapt to seasonal shifts in greenhouse environments.

## 7. Risks and Mitigations

| Risk | Mitigation |
|:---|:---|
| **Posterior Collapse** | Mitigated by using a low $\beta$ (0.1) to prevent the KL term from dominating the reconstruction loss. |
| **Synthetic-to-Real Gap** | Use of domain-specific data augmentation and realistic noise injection during training to bridge the gap between synthetic generators and real sensor streams. |
| **Sequence Length Sensitivity** | The use of a sliding window approach and the progressive compression in the LSTM layers provides robustness to slight variations in temporal dynamics. |
|

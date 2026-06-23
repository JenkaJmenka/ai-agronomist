# Week 1 Phase 1 MVP - Refactored Task Breakdown

## Smaller tasks using new agent configuration

---

## 📋 **New Task Structure**

Instead of 5 large tasks, we now have **9 smaller, focused tasks**:

| Phase                    | Task | Agent         | File              | Duration |
| ------------------------ | ---- | ------------- | ----------------- | -------- |
| **Phase 1 (Parallel)**   | 1.1  | architect     | Design docs       | 5 min    |
|                          | 1.2  | ml-engineer   | LSTM-VAE model    | 10 min   |
|                          | 1.3  | ml-engineer   | Data generator    | 10 min   |
|                          | 1.4  | devops-lead   | Docker setup      | 10 min   |
| **Phase 2 (Sequential)** | 1.5  | ml-engineer   | Configuration     | 5 min    |
|                          | 1.6  | ml-engineer   | Training pipeline | 10 min   |
| **Phase 3 (Sequential)** | 1.7  | qa-engineer   | Unit tests        | 10 min   |
| **Phase 4 (Final)**      | 1.8  | code-reviewer | Review & ADRs     | 10 min   |

**Total Time:** ~70 minutes  
**Parallelizable:** Phase 1 agents run simultaneously  
**Sequential:** Phase 2+ depend on previous output

---

## 🚀 **ORCHESTRATOR SUBMISSION COMMAND**

Copy this exact command to submit all Week 1 tasks:

```bash
# At orchestrator prompt:

@architect @ml-engineer @devops-lead
[paste Task 1.1, 1.2, 1.3, 1.4 below]

# Wait for Phase 1 to complete (~35 minutes)
# Then submit Phase 2

@ml-engineer
[paste Task 1.5 and 1.6 below]

# Wait for Phase 2 to complete (~15 minutes)
# Then submit Phase 3

@qa-engineer
[paste Task 1.7 below with all Phase 1+2 files as context]

# Wait for Phase 3 to complete (~10 minutes)
# Then submit Phase 4

@code-reviewer
[paste Task 1.8 below with all Phase 1-3 files as context]
```

---

# PHASE 1: PARALLEL TASKS (Run simultaneously)

---

## 📋 **TASK 1.1: LSTM-VAE Architecture & Design**

### Agent: architect

### Output: `docs/architecture/lstm_vae_design.md`

### Duration: 5 minutes

### Depends on: Nothing

```
You are the architect designing the LSTM-VAE anomaly detection system.
Your output is a design document — NOT implementation code.

## Task: Design the LSTM-VAE Architecture

Produce: docs/architecture/lstm_vae_design.md

This document should clearly specify:

1. LSTM-VAE Architecture Specification
   - Input shape and data semantics
   - Encoder architecture (layer count, hidden dims, sequence handling)
   - Latent space (dimensionality, distribution assumptions)
   - Decoder architecture (mirror of encoder)
   - Output shape and semantics

2. Loss Function Specification
   - Reconstruction loss formula and justification
   - KL divergence loss formula and weighting
   - Combined loss function
   - Why these choices for greenhouse anomaly detection?

3. Hyperparameter Defaults
   - Justified values for:
     - input_dim: 4 sensors (temperature, humidity, CO2, light)
     - hidden_dims: [64, 32] (why these sizes?)
     - latent_dim: 8 (why 8? why not 4 or 16?)
     - seq_len: 20 (why 20 timesteps?)
     - beta: 0.1 (weighting for KL loss, why 0.1?)
     - dropout: 0.2 (why 0.2?)

4. Training Assumptions
   - What data distribution are we assuming? (normal behavior only)
   - What are we trying to detect? (outliers from normal)
   - How will we measure success? (reconstruction error as anomaly score)

5. Phase 1.5 Extensibility
   - Can we easily swap synthetic data for real customer data?
   - Can we add new sensor types without redesigning?
   - Can we adapt the model to different crops/climates?

6. Constraints & Assumptions
   - CPU vs GPU target?
   - Latency requirements for inference?
   - Model size constraints?
   - Reproducibility requirements?

7. Risks & Mitigations
   - What could go wrong with this design?
   - What validation will we do?
   - Fallback plans if accuracy is low?

Use this format:

\`\`\`markdown
# LSTM-VAE Architecture Design

## 1. Overview
[1-2 sentence summary of what LSTM-VAE does]

## 2. Architecture Diagram
[ASCII art or text description showing:
Input → Encoder LSTM → Latent Distribution → Decoder LSTM → Output
]

## 3. Component Specifications

### Encoder
- Type: 2-layer LSTM
- Layer 1: [describe input, hidden dims, output]
- Layer 2: [describe input, hidden dims, output]
- Latent mapping: [μ and log(σ²) from hidden state]

### Latent Space
- Distribution: N(μ, diag(σ²))
- Dimensionality: 8
- Reparameterization: z = μ + σ ⊙ ε

### Decoder
- Type: 2-layer LSTM
- Takes latent vector z as input
- Expands back to (seq_len, input_dim)

## 4. Loss Function

**Reconstruction Loss:**
L_recon = MSE(x, x̂) = (1/N) Σ ||x_i - x̂_i||²

**KL Divergence Loss:**
L_KL = -0.5 Σ (1 + log(σ²) - μ² - σ²)

**Total Loss:**
L_total = L_recon + β * L_KL  (where β = 0.1)

Why β = 0.1?
- β too small: Model ignores KL, becomes deterministic autoencoder
- β too large: Model ignores reconstruction, collapses to N(0,I)
- β = 0.1: Good balance for anomaly detection

## 5. Hyperparameter Justification

| Parameter | Value | Justification |
|-----------|-------|---|
| input_dim | 4 | Temperature, Humidity, CO2, Light |
| hidden_dims | [64, 32] | 64→32 reduces dimensions gradually |
| latent_dim | 8 | Small enough for interpretability, large enough for expressiveness |
| seq_len | 20 | ~5-10 minutes at 15-30sec sampling rate |
| beta | 0.1 | Balanced reconstruction vs. regularization |
| dropout | 0.2 | Moderate regularization, prevent overfitting |

## 6. Success Criteria

- Reconstruction accuracy: >85% on validation set
- Training convergence: <100 epochs to convergence
- Inference latency: <50ms per sequence (CPU)
- Model size: <10MB

## 7. Phase 1.5 Extensibility

✓ Data loader can be swapped (synthetic → real)
✓ Sensor types configurable via input_dim
✓ Hyperparameters externalized in config.py
✓ Model checkpoints include all metadata

## 8. Known Limitations & Mitigations

| Risk | Mitigation |
|------|-----------|
| Synthetic ≠ Real data | Phase 2: transfer learning from customer data |
| Low anomaly accuracy | Phase 2: ensemble with Isolation Forest |
| GPU not available | Design for CPU first, optimize later |
| Overfitting on synthetic | Dropout + KL regularization + early stopping |

\`\`\`

Output this design document to: docs/architecture/lstm_vae_design.md

Do NOT write any Python code.
Do NOT implement anything.
Just design and explain the architecture.
```

---

## 📋 **TASK 1.2: LSTM-VAE Model Implementation**

### Agent: ml-engineer

### Output: `src/models/lstm_vae.py`

### Duration: 10 minutes

### Depends on: Task 1.1 (architecture design)

````
You are implementing the LSTM-VAE model specified in the architecture design.

Files to read:
- docs/architecture/lstm_vae_design.md (from architect)
- src/config.py (will be created, assume defaults: input_dim=4, hidden_dims=[64,32], latent_dim=8, seq_len=20, beta=0.1, dropout=0.2)

Task: Implement src/models/lstm_vae.py

This file should contain ONE class: LSTMVAE(nn.Module)

The class must have these methods (matching the architecture design):

```python
class LSTMVAE(nn.Module):
    def __init__(
        self,
        input_dim: int = 4,
        hidden_dims: List[int] = [64, 32],
        latent_dim: int = 8,
        seq_len: int = 20,
        beta: float = 0.1,
        dropout: float = 0.2
    ):
        """Initialize LSTM-VAE model"""

    def encode(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Encode sequence to latent distribution parameters.

        Input: (batch, seq_len, input_dim)
        Output: (mu, log_var) each (batch, latent_dim)
        """

    def decode(self, z: torch.Tensor) -> torch.Tensor:
        """
        Decode latent vector to sequence.

        Input: (batch, latent_dim)
        Output: (batch, seq_len, input_dim)
        """

    def reparameterize(self, mu: torch.Tensor, log_var: torch.Tensor) -> torch.Tensor:
        """
        Sample from latent distribution using reparameterization trick.

        z = mu + std * epsilon where epsilon ~ N(0, 1)
        """

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Full forward pass.

        Input: (batch, seq_len, input_dim)
        Output: (x_recon, mu, log_var, z)
        """

    def loss_function(self, x_recon: torch.Tensor, x: torch.Tensor,
                     mu: torch.Tensor, log_var: torch.Tensor) -> torch.Tensor:
        """
        Calculate VAE loss = reconstruction + beta * KL.

        Returns: scalar loss tensor
        """

    def save_checkpoint(self, path: str, epoch: int, loss: float, accuracy: float) -> None:
        """
        Save model checkpoint with metadata.

        Metadata: epoch, loss, accuracy, timestamp, model_config
        """

    def load_checkpoint(self, path: str) -> Dict:
        """
        Load checkpoint and return metadata dict.
        """
````

Requirements:
✓ Type hints on all functions
✓ Docstrings on all methods
✓ No hardcoded magic numbers (all in config or parameters)
✓ Device-agnostic (work on CPU and GPU)
✓ Comments explaining complex logic (e.g., reparameterization)
✓ Error handling (check input shapes, NaN detection)

Output this to: src/models/lstm_vae.py

Do NOT create tests yet.
Do NOT create Docker files.
Do NOT implement training pipeline.
Just the model class.

```

---

## 📋 **TASK 1.3: Synthetic Data Generator**
### Agent: ml-engineer
### Output: `src/models/data_generator.py`
### Duration: 10 minutes
### Depends on: Nothing (parallel with 1.2)

```

You are implementing the synthetic data generator for training.

Task: Implement src/models/data_generator.py

This file should contain ONE class: SyntheticDataGenerator

The class must:

```python
class SyntheticDataGenerator:
    def __init__(
        self,
        num_sequences: int = 1000,
        seq_len: int = 20,
        input_dim: int = 4,
        random_seed: int = 42
    ):
        """Initialize generator with configuration"""

    def generate_normal_data(self) -> np.ndarray:
        """
        Generate realistic greenhouse sensor time series.

        Returns: (num_sequences, seq_len, input_dim) array

        Requirements:
        - Sensor 0 (temperature): 20-30°C → normalize to [0, 1]
        - Sensor 1 (humidity): 50-80% → normalize to [0, 1]
        - Sensor 2 (CO2): 300-500 ppm → normalize to [0, 1]
        - Sensor 3 (light): 0-1000 lux with day/night cycle

        - Realistic temporal correlations (humidity ↔ temperature, etc.)
        - No NaN or Inf values
        - Reproducible with fixed random_seed
        - No anomalies (pure normal behavior)
        """

    def normalize_data(self, data: np.ndarray) -> Tuple[np.ndarray, Dict]:
        """
        Normalize data to [0, 1].

        Returns: (normalized_data, normalization_params)
        """

    def add_temporal_correlations(self, data: np.ndarray) -> np.ndarray:
        """
        Add correlations between sensors (e.g., temperature ↔ humidity).
        """
```

Requirements:
✓ Type hints
✓ Docstrings
✓ Fixed random seed produces identical data
✓ Data normalized to [0, 1]
✓ Realistic greenhouse ranges
✓ Temporal correlations
✓ No NaN/Inf

Output to: src/models/data_generator.py

Do NOT create config.py yet.
Do NOT create tests yet.

```

---

## 📋 **TASK 1.4: Docker Development Setup**
### Agent: devops-lead
### Output: `docker-compose.yml`, `Dockerfile`, `Dockerfile.jupyter`, `.env.development`, `.env.example`
### Duration: 10 minutes
### Depends on: Nothing (parallel with 1.2, 1.3)

```

You are setting up Docker for local development.

Task: Create Docker configuration files for local dev environment

Files to create:

1. docker-compose.yml - 4 services (PostgreSQL, Jupyter, TensorBoard, Adminer)
2. Dockerfile - Multi-stage build for main app
3. Dockerfile.jupyter - Jupyter Lab development environment
4. .env.development - Dev environment variables
5. .env.example - Template for deployment

Requirements:

docker-compose.yml:
Services:

- postgres:15-alpine (PORT 5432)
- jupyter (Dockerfile.jupyter, PORT 8888)
- tensorboard (PORT 6006)
- adminer (PORT 8080)

All services on agronomist-network
Health checks for postgres
Named volume for postgres_data persistence

Dockerfile:

- Multi-stage build (builder → runtime)
- Base: python:3.11-slim
- Copy requirements.txt from builder
- Create /app/logs, /app/models/checkpoints, /app/data/synthetic
- Non-root user (appuser)
- Health check
- ENV: PYTHONUNBUFFERED=1, DATABASE_URL, LOG_LEVEL

.env.development:

- DATABASE_URL=postgresql://postgres:password@postgres:5432/greenhouse_monitoring
- DEBUG=True
- DEVICE=cpu
- RANDOM_SEED=42

.env.example:

- Template with placeholders

.dockerignore:

- **pycache**, .git, .coverage, .pytest_cache/, logs/_, models/checkpoints/_, etc.

Do NOT:

- Don't implement Kubernetes
- Don't add CI/CD pipelines
- Don't add GPU support yet (Phase 2)

Output to: docker/, .env.development, .env.example, .dockerignore

```

---

# PHASE 2: SEQUENTIAL TASKS (Run after Phase 1)

---

## 📋 **TASK 1.5: Configuration Management**
### Agent: ml-engineer
### Output: `src/config.py`
### Duration: 5 minutes
### Depends on: Task 1.2, 1.3 (to know what config is needed)

```

You are creating centralized configuration for the entire Phase 1 system.

Files to read:

- src/models/lstm_vae.py (from Task 1.2)
- src/models/data_generator.py (from Task 1.3)

Task: Create src/config.py

This file should contain dictionaries for:

```python
# Model hyperparameters (for LSTM-VAE)
MODEL_CONFIG = {
    'input_dim': 4,
    'hidden_dims': [64, 32],
    'latent_dim': 8,
    'seq_len': 20,
    'beta': 0.1,
    'dropout': 0.2
}

# Data generation parameters
DATA_CONFIG = {
    'num_sequences': 1000,
    'seq_len': 20,
    'input_dim': 4,
    'random_seed': 42
}

# Training parameters
TRAINING_CONFIG = {
    'epochs': 100,
    'batch_size': 32,
    'learning_rate': 1e-3,
    'weight_decay': 1e-5,
    'early_stopping_patience': 10,
    'validation_split': 0.2
}

# Paths
PATHS = {
    'checkpoint_dir': 'models/checkpoints/',
    'data_dir': 'data/synthetic/',
    'log_dir': 'logs/tensorboard/'
}
```

Requirements:
✓ All hyperparameters externalized (no magic numbers in code)
✓ Sensible defaults
✓ Comments explaining why each value
✓ All values used elsewhere reference this file

Output to: src/config.py

```

---

## 📋 **TASK 1.6: Training Pipeline**
### Agent: ml-engineer
### Output: `src/models/training.py`, `scripts/train_lstm_vae.py`
### Duration: 10 minutes
### Depends on: Task 1.5 (need config)

```

You are implementing the complete training pipeline.

Files to read:

- src/models/lstm_vae.py (from Task 1.2)
- src/models/data_generator.py (from Task 1.3)
- src/config.py (from Task 1.5)

Task: Create TWO files:

1. src/models/training.py

This file implements:

```python
class VAETrainer:
    def __init__(
        self,
        model: LSTMVAE,
        train_loader: DataLoader,
        val_loader: DataLoader,
        learning_rate: float = 1e-3,
        weight_decay: float = 1e-5,
        device: str = 'cpu',
        checkpoint_dir: str = 'models/checkpoints/'
    ):
        """Initialize trainer"""

    def train_epoch(self, epoch: int) -> float:
        """
        Train one epoch. Return average loss.
        """

    def validate(self, epoch: int) -> float:
        """
        Validate on validation set. Return average loss.
        """

    def train(
        self,
        epochs: int = 100,
        early_stopping_patience: int = 10,
        checkpoint_interval: int = 5
    ) -> Dict:
        """
        Full training loop with early stopping.

        Returns: {
            'train_loss': [epoch losses],
            'val_loss': [epoch losses],
            'best_epoch': int,
            'best_val_loss': float,
            'training_time': float,
            'final_accuracy': float
        }
        """

    def calculate_accuracy(self, val_loader: DataLoader) -> float:
        """
        Calculate accuracy as % of samples with reconstruction error below threshold.
        """
```

2. scripts/train_lstm_vae.py

Command-line entry point:

```python
def main():
    """
    Main training script.

    Usage:
        python scripts/train_lstm_vae.py --epochs 100 --batch_size 32

    Arguments:
        --epochs: Number of epochs
        --batch_size: Batch size
        --lr: Learning rate
        --device: 'cpu' or 'cuda'
        --checkpoint_dir: Where to save checkpoints
    """
```

Requirements:
✓ Type hints
✓ Docstrings
✓ Error handling (device placement, NaN detection)
✓ Logging informative messages
✓ Config read from src/config.py, not hardcoded
✓ Checkpoint save includes: epoch, loss, accuracy, timestamp, model_config

Output to: src/models/training.py, scripts/train_lstm_vae.py

```

---

# PHASE 3: QA GATE

---

## 📋 **TASK 1.7: Unit Tests**
### Agent: qa-engineer
### Output: `tests/unit/test_lstm_vae.py`, `tests/unit/test_data_generator.py`, `tests/integration/test_training.py`
### Duration: 10 minutes
### Depends on: Task 1.2, 1.3, 1.6 (needs implementation code)

```

You are the QA engineer. Write comprehensive tests for all Phase 1 code.

Files to read (implementation code to test):

- src/models/lstm_vae.py
- src/models/data_generator.py
- src/models/training.py
- src/config.py

Task: Create test files with >80% coverage

1. tests/unit/test_lstm_vae.py

Test cases:

- Model instantiation
- Forward pass shapes (input → output)
- No NaN/Inf in outputs
- Loss calculation
- Checkpoint save/load with metadata
- Reparameterization stochasticity
- Device placement (CPU/GPU)

2. tests/unit/test_data_generator.py

Test cases:

- Generator instantiation
- Data shape correct (1000, 20, 4)
- Data range [0, 1]
- No NaN/Inf
- Reproducibility (same seed = same data)
- Different seed = different data
- Temporal correlations exist

3. tests/integration/test_training.py

Test cases:

- Training reduces loss over epochs
- Validation loss computed
- Checkpoints saved and loaded
- Early stopping works
- Training history returned correctly

4. pytest.ini and .coveragerc

Configure pytest and coverage:

- pytest.ini: testpaths, markers, cov-fail-under=80
- .coveragerc: source=src, precision=2

Requirements:
✓ >80% code coverage overall
✓ Critical paths >90% coverage (model forward, loss function, training loop)
✓ Edge cases tested (empty batches, NaN handling, etc.)
✓ Fixtures for common test data
✓ Clear test names (test_forward_pass_shapes, etc.)

Output to: tests/unit/test_lstm_vae.py, tests/unit/test_data_generator.py,
tests/integration/test_training.py, pytest.ini, .coveragerc

Do NOT fix any bugs you find.
Report them in the test output.

```

---

# PHASE 4: FINAL REVIEW

---

## 📋 **TASK 1.8: Code Review & Architecture Decision Records**
### Agent: code-reviewer
### Output: `docs/review/week1-review.md`, `docs/architecture/ADR-*.md`
### Duration: 10 minutes
### Depends on: All Phase 1-3 tasks

```

You are the code reviewer. Perform final quality gate on all Week 1 code.

Files to read (all implementation):

- src/models/lstm_vae.py
- src/models/data_generator.py
- src/models/training.py
- scripts/train_lstm_vae.py
- src/config.py
- docker-compose.yml
- Dockerfile
- tests/unit/test_lstm_vae.py
- tests/unit/test_data_generator.py
- tests/integration/test_training.py

Task: Produce TWO outputs

1. docs/review/week1-review.md

Review checklist:

- Code quality (type hints, docstrings, comments)
- Security (no secrets, no shell injections, proper error handling)
- Performance (efficient tensor ops, no unnecessary copying)
- Correctness (loss function correct? checkpoint integrity? data generation accurate?)
- Testing (coverage adequate? edge cases covered?)
- Architecture (loose coupling? extensible for Phase 1.5?)

Format:

\`\`\`markdown

# Week 1 Code Review

## Overall Status

[PASS / PASS WITH NOTES / NEEDS FIXES]

## By Component

### LSTM-VAE Model (src/models/lstm_vae.py)

Status: [✓/✗]
Issues: [list any issues]
Observations: [what went well]

### Data Generator (src/models/data_generator.py)

Status: [✓/✗]
Issues: [...]
Observations: [...]

### Training Pipeline (src/models/training.py)

Status: [✓/✗]
Issues: [...]
Observations: [...]

### Unit Tests (tests/)

Status: [✓/✗]
Coverage: X%
Issues: [...]

### Docker Setup

Status: [✓/✗]
Issues: [...]

## Guardrails Check

- [ ] Type hints on all functions
- [ ] No hardcoded magic numbers
- [ ] No secrets in code
- [ ] Error handling present
- [ ] Docstrings complete
- [ ] Tests >80% coverage
- [ ] No circular dependencies
- [ ] Configuration externalized

## Blocking Issues

[Critical issues that must be fixed before merge]

## Non-Blocking Issues

[Nice-to-have improvements]

## Recommendations for Phase 2

[Suggestions for next phase]

\`\`\`

2. docs/architecture/ADR-\*.md

File three Architecture Decision Records:

ADR-001: LSTM-VAE Architecture Choice

- Why LSTM-VAE over alternatives?
- Trade-offs
- Scalability concerns

ADR-002: Synthetic Data Generation Strategy

- Why synthetic data in Phase 1?
- Limitations and Phase 1.5 transition plan

ADR-003: Modular Design for Phase 1.5

- How does this design enable multi-customer scaling?
- What's extensible? What might need refactoring?

Output to: docs/review/week1-review.md, docs/architecture/ADR-001-_.md,
docs/architecture/ADR-002-_.md, docs/architecture/ADR-003-\*.md

Read-only tools only (no fixes, no edits).
This is the final gate before merge.

```

---

## 📊 **Task Timeline**

```

Phase 1 (Parallel) — Start all at once, wait for all to complete
├── Task 1.1: architect (5 min) → lstm_vae_design.md
├── Task 1.2: ml-engineer (10 min) → lstm_vae.py
├── Task 1.3: ml-engineer (10 min) → data_generator.py
└── Task 1.4: devops-lead (10 min) → docker-compose.yml, Dockerfile, .env

Phase 2 (Sequential) — After Phase 1 complete
├── Task 1.5: ml-engineer (5 min) → config.py
└── Task 1.6: ml-engineer (10 min) → training.py, train_lstm_vae.py

Phase 3 (QA Gate) — After Phase 2 complete
└── Task 1.7: qa-engineer (10 min) → test\_\*.py, pytest.ini, .coveragerc

Phase 4 (Final) — After Phase 3 complete
└── Task 1.8: code-reviewer (10 min) → week1-review.md, ADR-\*.md

Total: ~70 minutes
Parallelizable: Phase 1 saves ~25 minutes vs sequential

````

---

## ✅ **How to Submit to Orchestrator**

### **PHASE 1: Submit all at once (parallel)**

```bash
# At orchestrator prompt:

@architect @ml-engineer @ml-engineer @devops-lead

TASK 1.1 (architect): [copy Task 1.1 above]
TASK 1.2 (ml-engineer): [copy Task 1.2 above]
TASK 1.3 (ml-engineer): [copy Task 1.3 above]
TASK 1.4 (devops-lead): [copy Task 1.4 above]

auto-proceed
````

### **PHASE 2: After Phase 1 complete**

```bash
@ml-engineer

TASK 1.5 (config): [copy Task 1.5 above]
TASK 1.6 (training): [copy Task 1.6 above]

auto-proceed
```

### **PHASE 3: After Phase 2 complete**

```bash
@qa-engineer

TASK 1.7 (tests): [copy Task 1.7 above]

INCLUDE AS CONTEXT:
[Paste all code from Phase 1+2]

auto-proceed
```

### **PHASE 4: After Phase 3 complete**

```bash
@code-reviewer

TASK 1.8 (review): [copy Task 1.8 above]

INCLUDE AS CONTEXT:
[Paste all code from Phase 1-3]

auto-proceed
```

---

## 🎯 **Success Criteria**

✅ All Phase 1 files created without errors  
✅ Phase 2 config and training complete  
✅ Phase 3 tests passing with >80% coverage  
✅ Phase 4 review status: PASS or PASS WITH NOTES  
✅ All 3 ADRs filed  
✅ No blocking issues  
✅ Week 1 complete, ready for Week 2 (REST API)

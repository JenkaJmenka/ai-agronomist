# Week 1 Phase 1 MVP - ML Foundation Tasks
## Decoupled Daily Prompts for Multi-Agent Orchestrator

**Goal:** Build the ML foundation for greenhouse anomaly detection  
**Duration:** Week 1 (Monday-Friday)  
**Success Criteria:** LSTM-VAE trained (70%+ accuracy), Docker environment working, >80% test coverage

---

## 📅 **Week 1 Schedule**

| Day | Task | Agent | Duration | Output |
|-----|------|-------|----------|--------|
| **Monday** | LSTM-VAE Architecture | Backend + ML | 20 min | Model class + synthetic data |
| **Tuesday** | Training Pipeline | ML | 15 min | Training script + TensorBoard |
| **Wednesday** | Unit Tests | QA | 10 min | Test suite (>80% coverage) |
| **Thursday** | Local Dev Environment | DevOps | 10 min | Docker Compose setup |
| **Friday** | Architecture Review | Architect + Code Review | 10 min | ADRs + code quality |

**Total Time:** ~65 minutes

---

# 📋 TASK 1: LSTM-VAE Architecture & Synthetic Data Generator
## Monday Morning - Stage 1 (Gemma Code Generation)

### **Task Type:** Core ML Component
### **Agents:** Backend Engineer (code structure) + ML Engineer (model implementation)
### **Estimated Time:** 20 minutes
### **Success Criteria:** Working LSTM-VAE class + data generator, no syntax errors

---

## **COMPLETE TASK PROMPT FOR STAGE 1**

```
Generate the LSTM-VAE architecture and synthetic data generator for greenhouse 
anomaly detection. This is the foundation for Phase 1 MVP.

## 1. LSTM-VAE Model Class

Create src/models/lstm_vae.py with complete LSTM-VAE implementation:

### Model Architecture
- Framework: PyTorch
- Encoder: LSTM layers (sequence → latent space)
  - Input: Time series of N sensor readings (shape: [batch, seq_len, input_dim])
  - LSTM layers: 2 layers with hidden sizes [64, 32]
  - Output: Mean and log-variance for latent space (8-dimensional)
  
- Decoder: LSTM layers (latent space → sequence)
  - Input: Latent vector (8-dimensional)
  - LSTM layers: 2 layers (mirror of encoder)
  - Output: Reconstructed sensor readings (same shape as input)

- Latent Space: 8-dimensional continuous representation

### Loss Function
- Reconstruction Loss: Mean Squared Error between input and reconstructed output
- KL Divergence Loss: Kullback-Leibler divergence between learned and standard normal
- Total Loss = reconstruction_loss + beta * kl_loss (beta=0.1 default)

### Model Class Requirements
```python
class LSTMVAE(nn.Module):
    def __init__(
        self,
        input_dim: int = 4,           # temperature, humidity, co2, light
        hidden_dims: List[int] = [64, 32],
        latent_dim: int = 8,
        seq_len: int = 20,            # 20 time steps
        beta: float = 0.1,
        dropout: float = 0.2
    ):
        # Initialize encoder, decoder, latent layers
        
    def encode(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        # Return mu, log_var (latent distribution parameters)
        
    def decode(self, z: torch.Tensor) -> torch.Tensor:
        # Return reconstructed sequence from latent vector
        
    def reparameterize(self, mu: torch.Tensor, log_var: torch.Tensor) -> torch.Tensor:
        # Sample from latent distribution using reparameterization trick
        
    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        # Return: reconstructed_x, mu, log_var, latent_z
        
    def save_checkpoint(self, path: str, epoch: int, loss: float, accuracy: float) -> None:
        # Save model with metadata (epoch, loss, date, git commit)
        # Metadata must include: epoch, loss, accuracy, timestamp, model_config
        
    def load_checkpoint(self, path: str) -> Dict:
        # Load model and return metadata
        
    def loss_function(self, recon_x: torch.Tensor, x: torch.Tensor, 
                      mu: torch.Tensor, log_var: torch.Tensor) -> torch.Tensor:
        # Calculate: MSE(recon, input) + KL_divergence(mu, log_var)
        return reconstruction_loss + self.beta * kl_loss
```

### Requirements
- Type hints on all methods
- Comprehensive docstrings (what inputs, outputs, behavior)
- Default hyperparameters reasonable for greenhouse data
- No magic numbers (all justified in comments)
- Reproducible: Fixed random seed support
- Device agnostic (CPU/GPU)

---

## 2. Synthetic Data Generator

Create src/models/data_generator.py with synthetic data generation:

### Generator Purpose
Generate realistic greenhouse sensor time series data for training.
Must simulate normal operation (no anomalies) to train baseline behavior.

### Sensor Data Characteristics
- Temperature: 20-30°C (normal greenhouse range)
- Humidity: 50-80% (typical greenhouse range)
- CO2: 300-500 ppm (normal levels)
- Light: 0-1000 lux (simulated day/night cycles)

### Generation Strategy (TimeGAN-inspired)
```python
class SyntheticDataGenerator:
    def __init__(
        self,
        num_sequences: int = 1000,    # Number of time series to generate
        seq_len: int = 20,             # Length of each sequence (20 timesteps)
        input_dim: int = 4,            # 4 sensor types
        random_seed: int = 42          # For reproducibility
    ):
        self.random_seed = random_seed
        np.random.seed(random_seed)
        torch.manual_seed(random_seed)
        
    def generate_normal_data(self) -> np.ndarray:
        """
        Generate realistic normal greenhouse sensor data.
        
        Returns:
            np.ndarray of shape (num_sequences, seq_len, input_dim)
        
        Simulation approach:
        1. Start with baseline values (center of normal range)
        2. Add smooth variations (temperature gradients, humidity changes)
        3. Add noise (sensor measurement noise, ~2% of value)
        4. Add day/night cycle (light intensity pattern)
        5. Ensure physical constraints (no negative values, within bounds)
        """
        
    def add_temporal_correlations(self, data: np.ndarray) -> np.ndarray:
        """
        Add correlations between sensors (temperature affects humidity, etc.)
        """
        
    def normalize_data(self, data: np.ndarray) -> Tuple[np.ndarray, Dict]:
        """
        Normalize to [0, 1] range for training.
        
        Returns:
            normalized_data, normalization_params (for denormalization)
        """
        
    def to_torch_tensors(self, data: np.ndarray) -> torch.Tensor:
        """Convert to PyTorch tensor"""
```

### Data Statistics
Must generate data with these characteristics:
- Temperature: mean=25°C, std=3°C, range=[20, 30]°C
- Humidity: mean=65%, std=10%, range=[50, 80]%
- CO2: mean=400 ppm, std=50 ppm, range=[300, 500] ppm
- Light: 0-1000 lux with day/night pattern

### Output Format
```python
# Returns:
# - Shape: (1000, 20, 4)  # 1000 sequences, 20 timesteps, 4 sensors
# - Type: np.ndarray or torch.Tensor
# - Range: [0, 1] after normalization
# - No NaN values
# - No constant sequences
```

### Requirements
- Reproducible (fixed random seed = same data every run)
- Realistic correlations between sensors
- Physical constraints enforced
- Normalized to [0, 1] for training
- No anomalies (pure normal behavior)
- Comprehensive docstrings explaining generation logic

---

## 3. Data Utilities

Create src/models/data_utils.py with helper functions:

```python
def load_greenhouse_data(path: str) -> Tuple[np.ndarray, Dict]:
    """Load from CSV or generate synthetic data"""
    
def preprocess_sequences(data: np.ndarray, seq_len: int = 20) -> np.ndarray:
    """Convert raw time series to sequences of fixed length"""
    
def create_data_loaders(
    train_data: np.ndarray,
    batch_size: int = 32,
    train_split: float = 0.8
) -> Tuple[DataLoader, DataLoader]:
    """Create PyTorch DataLoaders for training and validation"""
    
def denormalize_data(normalized: np.ndarray, params: Dict) -> np.ndarray:
    """Reverse normalization to original scale"""
```

---

## 4. Configuration

Create src/config.py with centralized configuration:

```python
# Model hyperparameters
MODEL_CONFIG = {
    'input_dim': 4,
    'hidden_dims': [64, 32],
    'latent_dim': 8,
    'seq_len': 20,
    'beta': 0.1,
    'dropout': 0.2
}

# Training hyperparameters
TRAINING_CONFIG = {
    'epochs': 100,
    'batch_size': 32,
    'learning_rate': 1e-3,
    'weight_decay': 1e-5,
    'early_stopping_patience': 10,
    'validation_split': 0.2
}

# Data generation
DATA_CONFIG = {
    'num_sequences': 1000,
    'seq_len': 20,
    'random_seed': 42
}

# Paths
PATHS = {
    'checkpoint_dir': 'models/checkpoints/',
    'data_dir': 'data/synthetic/',
    'log_dir': 'logs/tensorboard/'
}
```

---

## 5. Deliverables for Stage 1

### Code Files (to be created)
- [ ] `src/models/lstm_vae.py` - LSTM-VAE class
- [ ] `src/models/data_generator.py` - Synthetic data generator
- [ ] `src/models/data_utils.py` - Data utilities
- [ ] `src/config.py` - Configuration

### Documentation
- [ ] Docstrings on all classes and methods
- [ ] Type hints on all functions
- [ ] Comments explaining complex logic
- [ ] README explaining architecture

### Code Quality
- [ ] No magic numbers (all in config)
- [ ] Reproducible with fixed seed
- [ ] Type hints complete
- [ ] Docstrings comprehensive
- [ ] No circular dependencies

### What NOT to do
- ❌ Do NOT create files yet (only code output)
- ❌ Do NOT run tests (that's Wednesday)
- ❌ Do NOT train model (that's Tuesday)
- ❌ Do NOT create Docker (that's Thursday)

---

## SUBMISSION INSTRUCTIONS

### Stage 1: Code Generation (Gemma)
```bash
# At orchestrator "You: " prompt:
@backend-engineer @ml-engineer

[Paste this entire task (sections 1-5 above)]
```

Output will be:
- Complete LSTM-VAE implementation
- Synthetic data generator
- Data utilities
- Configuration file
- All in markdown code blocks with filenames

### Stage 2: File Creation (Sonnet)
After Stage 1 completes, copy the code output and submit:

```bash
@integrator

[Paste Stage 1 code output]

Please:
1. Create all files in src/models/ and src/
2. Verify no syntax errors
3. Test imports work
4. Report status
```

---

## EXPECTED OUTPUT

After Stage 1, you should have:

```
src/
├── models/
│   ├── lstm_vae.py (250-300 lines)
│   ├── data_generator.py (200-250 lines)
│   └── data_utils.py (100-150 lines)
└── config.py (50-75 lines)
```

All code will be:
- ✅ Production-ready
- ✅ Fully documented
- ✅ Type-hinted
- ✅ Reproducible
- ✅ Ready for training

---

## SUCCESS CRITERIA

✅ LSTM-VAE class instantiates without errors
✅ Forward pass works (input shape → output shape)
✅ Data generator produces correct shapes
✅ Data is normalized [0, 1]
✅ All docstrings present
✅ All type hints present
✅ Code follows PEP 8
✅ No dependencies missing

```

---


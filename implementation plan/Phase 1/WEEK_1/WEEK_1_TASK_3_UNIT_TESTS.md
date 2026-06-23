# 📋 TASK 3: Unit Tests & Model Validation
## Wednesday Morning - Stage 1 (Gemma Code Generation)

### **Task Type:** Testing & Validation
### **Agents:** QA Engineer (primary)
### **Estimated Time:** 10 minutes
### **Success Criteria:** >80% code coverage, all tests passing

### **Depends On:** Task 1 & 2 completion (model code exists)

---

## **COMPLETE TASK PROMPT FOR STAGE 1**

```
Generate comprehensive unit tests for LSTM-VAE model and training pipeline.
Tests must verify correctness and achieve >80% code coverage.

## 1. Unit Tests for LSTM-VAE Model

Create tests/unit/test_lstm_vae.py:

### Test Structure

```python
import pytest
import torch
import numpy as np
from src.models.lstm_vae import LSTMVAE
from src.config import MODEL_CONFIG

class TestLSTMVAEArchitecture:
    """Test LSTM-VAE model architecture and basic functionality"""
    
    @pytest.fixture
    def model(self):
        """Create model instance"""
        return LSTMVAE(**MODEL_CONFIG)
    
    @pytest.fixture
    def input_batch(self):
        """Create sample input batch"""
        batch_size = 32
        seq_len = MODEL_CONFIG['seq_len']
        input_dim = MODEL_CONFIG['input_dim']
        return torch.randn(batch_size, seq_len, input_dim)
    
    def test_model_instantiation(self, model):
        """Test model can be created without errors"""
        assert model is not None
        assert isinstance(model, LSTMVAE)
    
    def test_forward_pass_shapes(self, model, input_batch):
        """Test forward pass produces correct output shapes"""
        # Forward pass
        recon_x, mu, log_var, z = model(input_batch)
        
        # Check shapes
        assert recon_x.shape == input_batch.shape, f"Reconstruction shape mismatch"
        assert mu.shape == (input_batch.shape[0], MODEL_CONFIG['latent_dim'])
        assert log_var.shape == (input_batch.shape[0], MODEL_CONFIG['latent_dim'])
        assert z.shape == (input_batch.shape[0], MODEL_CONFIG['latent_dim'])
    
    def test_forward_pass_no_nan(self, model, input_batch):
        """Test forward pass doesn't produce NaN values"""
        recon_x, mu, log_var, z = model(input_batch)
        
        assert not torch.isnan(recon_x).any(), "NaN in reconstruction"
        assert not torch.isnan(mu).any(), "NaN in mu"
        assert not torch.isnan(log_var).any(), "NaN in log_var"
        assert not torch.isnan(z).any(), "NaN in latent"
    
    def test_reconstruction_bounded(self, model, input_batch):
        """Test reconstruction is bounded (between 0 and 1 for normalized data)"""
        recon_x, _, _, _ = model(input_batch)
        
        # For normalized data
        assert (recon_x >= -0.1).all(), "Reconstruction below min"  # Allow small overshoot
        assert (recon_x <= 1.1).all(), "Reconstruction above max"
    
    def test_encode(self, model, input_batch):
        """Test encoder separately"""
        mu, log_var = model.encode(input_batch)
        
        assert mu.shape == (input_batch.shape[0], MODEL_CONFIG['latent_dim'])
        assert log_var.shape == (input_batch.shape[0], MODEL_CONFIG['latent_dim'])
        assert not torch.isnan(mu).any()
        assert not torch.isnan(log_var).any()
    
    def test_decode(self, model):
        """Test decoder separately"""
        batch_size = 32
        latent_dim = MODEL_CONFIG['latent_dim']
        z = torch.randn(batch_size, latent_dim)
        
        recon = model.decode(z)
        
        assert recon.shape == (batch_size, MODEL_CONFIG['seq_len'], MODEL_CONFIG['input_dim'])
        assert not torch.isnan(recon).any()
    
    def test_reparameterization(self, model, input_batch):
        """Test reparameterization trick"""
        mu, log_var = model.encode(input_batch)
        z = model.reparameterize(mu, log_var)
        
        assert z.shape == mu.shape
        assert not torch.isnan(z).any()
        
        # Reparameterization should add stochasticity
        z2 = model.reparameterize(mu, log_var)
        assert not torch.allclose(z, z2), "Reparameterization should be stochastic"
    
    def test_loss_calculation(self, model, input_batch):
        """Test loss function"""
        recon_x, mu, log_var, _ = model(input_batch)
        loss = model.loss_function(recon_x, input_batch, mu, log_var)
        
        assert loss.item() > 0, "Loss should be positive"
        assert not torch.isnan(loss), "Loss should not be NaN"
    
    def test_loss_components(self, model, input_batch):
        """Test that loss has reconstruction and KL components"""
        recon_x, mu, log_var, _ = model(input_batch)
        loss = model.loss_function(recon_x, input_batch, mu, log_var)
        
        # Loss should be sum of reconstruction and KL
        # Reconstruction loss (MSE) should be reasonable
        recon_loss = torch.nn.functional.mse_loss(recon_x, input_batch)
        assert recon_loss > 0
        
        # Total loss should be >= reconstruction loss (due to KL term)
        assert loss >= recon_loss


class TestCheckpointSaving:
    """Test model checkpoint saving and loading"""
    
    @pytest.fixture
    def model(self):
        return LSTMVAE(**MODEL_CONFIG)
    
    @pytest.fixture
    def temp_dir(self, tmp_path):
        """Create temporary directory for checkpoints"""
        return tmp_path / "checkpoints"
    
    def test_checkpoint_save(self, model, temp_dir):
        """Test saving checkpoint"""
        temp_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = temp_dir / "test_checkpoint.pt"
        
        model.save_checkpoint(
            path=str(checkpoint_path),
            epoch=1,
            loss=0.05,
            accuracy=85.0
        )
        
        assert checkpoint_path.exists(), "Checkpoint file not created"
    
    def test_checkpoint_metadata(self, model, temp_dir):
        """Test checkpoint contains metadata"""
        temp_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = temp_dir / "test_checkpoint.pt"
        
        model.save_checkpoint(
            path=str(checkpoint_path),
            epoch=5,
            loss=0.03,
            accuracy=90.0
        )
        
        checkpoint = torch.load(checkpoint_path)
        
        assert 'epoch' in checkpoint
        assert 'loss' in checkpoint
        assert 'accuracy' in checkpoint
        assert 'timestamp' in checkpoint
        assert checkpoint['epoch'] == 5
        assert checkpoint['loss'] == 0.03
        assert checkpoint['accuracy'] == 90.0
    
    def test_checkpoint_load(self, model, temp_dir):
        """Test loading checkpoint"""
        temp_dir.mkdir(parents=True, exist_ok=True)
        checkpoint_path = temp_dir / "test_checkpoint.pt"
        
        model.save_checkpoint(
            path=str(checkpoint_path),
            epoch=10,
            loss=0.02,
            accuracy=95.0
        )
        
        metadata = model.load_checkpoint(str(checkpoint_path))
        
        assert metadata['epoch'] == 10
        assert metadata['loss'] == 0.02
        assert metadata['accuracy'] == 95.0
```

---

## 2. Unit Tests for Data Generator

Create tests/unit/test_data_generator.py:

```python
import pytest
import numpy as np
from src.models.data_generator import SyntheticDataGenerator
from src.config import DATA_CONFIG

class TestSyntheticDataGenerator:
    """Test synthetic data generation"""
    
    @pytest.fixture
    def generator(self):
        return SyntheticDataGenerator(**DATA_CONFIG)
    
    def test_generator_instantiation(self, generator):
        """Test generator can be created"""
        assert generator is not None
    
    def test_generate_data_shape(self, generator):
        """Test generated data has correct shape"""
        data = generator.generate_normal_data()
        
        assert data.shape[0] == DATA_CONFIG['num_sequences'], "Wrong number of sequences"
        assert data.shape[1] == DATA_CONFIG['seq_len'], "Wrong sequence length"
        assert data.shape[2] == DATA_CONFIG['input_dim'], "Wrong number of features"
    
    def test_generate_data_no_nan(self, generator):
        """Test generated data has no NaN values"""
        data = generator.generate_normal_data()
        assert not np.isnan(data).any(), "Data contains NaN values"
    
    def test_generate_data_no_inf(self, generator):
        """Test generated data has no infinite values"""
        data = generator.generate_normal_data()
        assert not np.isinf(data).any(), "Data contains infinite values"
    
    def test_generate_data_bounds(self, generator):
        """Test generated data is within expected bounds"""
        data = generator.generate_normal_data()
        
        # Normalized data should be roughly [0, 1]
        assert data.min() >= -0.1, f"Min value too low: {data.min()}"
        assert data.max() <= 1.1, f"Max value too high: {data.max()}"
    
    def test_generate_data_variance(self, generator):
        """Test generated data has variance (not constant)"""
        data = generator.generate_normal_data()
        
        # Each sequence should have variance
        for i in range(data.shape[0]):
            sequence_var = np.var(data[i])
            assert sequence_var > 0.001, f"Sequence {i} has no variance"
    
    def test_reproducibility(self):
        """Test that fixed seed produces same data"""
        gen1 = SyntheticDataGenerator(**{**DATA_CONFIG, 'random_seed': 42})
        gen2 = SyntheticDataGenerator(**{**DATA_CONFIG, 'random_seed': 42})
        
        data1 = gen1.generate_normal_data()
        data2 = gen2.generate_normal_data()
        
        np.testing.assert_array_almost_equal(data1, data2, decimal=6)
    
    def test_different_seeds_produce_different_data(self):
        """Test that different seeds produce different data"""
        gen1 = SyntheticDataGenerator(**{**DATA_CONFIG, 'random_seed': 42})
        gen2 = SyntheticDataGenerator(**{**DATA_CONFIG, 'random_seed': 123})
        
        data1 = gen1.generate_normal_data()
        data2 = gen2.generate_normal_data()
        
        # Should be different
        assert not np.allclose(data1, data2), "Different seeds should produce different data"
    
    def test_temporal_correlations(self, generator):
        """Test that temporal correlations exist"""
        data = generator.generate_normal_data()
        
        # Check autocorrelation (consecutive values should be correlated)
        correlations = []
        for i in range(data.shape[0]):
            sequence = data[i]
            # Correlation between t and t+1
            corr = np.corrcoef(sequence[:-1].flatten(), sequence[1:].flatten())[0, 1]
            if not np.isnan(corr):
                correlations.append(corr)
        
        avg_corr = np.mean(correlations)
        assert avg_corr > 0.5, "Temporal correlations too weak"
```

---

## 3. Unit Tests for Data Utils

Create tests/unit/test_data_utils.py:

```python
import pytest
import torch
import numpy as np
from torch.utils.data import DataLoader
from src.models.data_utils import (
    preprocess_sequences,
    create_data_loaders,
    denormalize_data
)

class TestDataUtils:
    """Test data utility functions"""
    
    def test_create_data_loaders(self):
        """Test DataLoader creation"""
        # Create sample data
        data = np.random.rand(100, 20, 4)  # 100 sequences
        
        train_loader, val_loader = create_data_loaders(
            data,
            batch_size=32,
            train_split=0.8
        )
        
        assert isinstance(train_loader, DataLoader)
        assert isinstance(val_loader, DataLoader)
        
        # Check sizes
        assert len(train_loader.dataset) == 80  # 80% of 100
        assert len(val_loader.dataset) == 20   # 20% of 100
    
    def test_loader_batch_shape(self):
        """Test DataLoader produces correct batch shapes"""
        data = np.random.rand(100, 20, 4)
        train_loader, _ = create_data_loaders(data, batch_size=32)
        
        batch = next(iter(train_loader))
        
        # Batch should be (batch_size, seq_len, input_dim)
        assert batch.shape[0] <= 32
        assert batch.shape[1] == 20
        assert batch.shape[2] == 4
    
    def test_loader_outputs_tensors(self):
        """Test DataLoader outputs PyTorch tensors"""
        data = np.random.rand(100, 20, 4)
        train_loader, _ = create_data_loaders(data, batch_size=32)
        
        batch = next(iter(train_loader))
        
        assert isinstance(batch, torch.Tensor)
```

---

## 4. Integration Tests

Create tests/integration/test_model_training.py:

```python
import pytest
import torch
from src.models.lstm_vae import LSTMVAE
from src.models.data_generator import SyntheticDataGenerator
from src.models.data_utils import create_data_loaders
from src.models.training import VAETrainer
from src.config import MODEL_CONFIG, DATA_CONFIG, TRAINING_CONFIG

class TestEndToEnd:
    """End-to-end training test"""
    
    def test_training_reduces_loss(self, tmp_path):
        """Test that training actually reduces loss"""
        # Generate small dataset
        gen = SyntheticDataGenerator(
            num_sequences=100,
            seq_len=20,
            input_dim=4,
            random_seed=42
        )
        data = gen.generate_normal_data()
        
        # Create loaders
        train_loader, val_loader = create_data_loaders(
            data,
            batch_size=32,
            train_split=0.8
        )
        
        # Create model
        model = LSTMVAE(**MODEL_CONFIG)
        
        # Create trainer
        trainer = VAETrainer(
            model=model,
            train_loader=train_loader,
            val_loader=val_loader,
            learning_rate=TRAINING_CONFIG['learning_rate'],
            device='cpu',
            checkpoint_dir=str(tmp_path),
            log_dir=str(tmp_path / 'logs')
        )
        
        # Train for a few epochs
        history = trainer.train(epochs=5, early_stopping_patience=10)
        
        # Check that loss decreased
        initial_loss = history['train_loss'][0]
        final_loss = history['train_loss'][-1]
        
        assert final_loss < initial_loss, "Loss should decrease during training"
    
    def test_accuracy_increases(self, tmp_path):
        """Test that accuracy increases during training"""
        gen = SyntheticDataGenerator(
            num_sequences=100,
            seq_len=20,
            input_dim=4,
            random_seed=42
        )
        data = gen.generate_normal_data()
        
        train_loader, val_loader = create_data_loaders(data, batch_size=32)
        
        model = LSTMVAE(**MODEL_CONFIG)
        
        trainer = VAETrainer(
            model=model,
            train_loader=train_loader,
            val_loader=val_loader,
            device='cpu',
            checkpoint_dir=str(tmp_path),
            log_dir=str(tmp_path / 'logs')
        )
        
        history = trainer.train(epochs=10)
        
        assert history['final_accuracy'] > 50, "Should achieve >50% accuracy"
```

---

## 5. Test Configuration

Create tests/conftest.py with pytest fixtures:

```python
import pytest
import torch
from src.models.lstm_vae import LSTMVAE
from src.config import MODEL_CONFIG

@pytest.fixture
def device():
    """Get test device (CPU for consistency)"""
    return 'cpu'

@pytest.fixture
def model(device):
    """Create model instance for testing"""
    model = LSTMVAE(**MODEL_CONFIG)
    model = model.to(device)
    return model

@pytest.fixture
def sample_input():
    """Create sample input"""
    batch_size = 8
    seq_len = MODEL_CONFIG['seq_len']
    input_dim = MODEL_CONFIG['input_dim']
    return torch.randn(batch_size, seq_len, input_dim)
```

---

## 6. Coverage Configuration

Create .coveragerc:

```ini
[run]
source = src
branch = True

[report]
precision = 2
show_missing = True
skip_covered = False

exclude_lines =
    pragma: no cover
    def __repr__
    raise AssertionError
    raise NotImplementedError
    if __name__ == .__main__.:
    if TYPE_CHECKING:
    @abstractmethod
```

---

## 7. Pytest Configuration

Create pytest.ini:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
addopts = 
    -v
    --tb=short
    --strict-markers
    --cov=src
    --cov-report=html
    --cov-report=term-missing
    --cov-fail-under=80

markers =
    unit: Unit tests
    integration: Integration tests
    slow: Slow tests
```

---

## 8. Test Deliverables

### Test Files (to be created)
- [ ] `tests/unit/test_lstm_vae.py` (200+ lines)
- [ ] `tests/unit/test_data_generator.py` (150+ lines)
- [ ] `tests/unit/test_data_utils.py` (100+ lines)
- [ ] `tests/integration/test_model_training.py` (150+ lines)
- [ ] `tests/conftest.py` (50+ lines)
- [ ] `.coveragerc` (configuration)
- [ ] `pytest.ini` (configuration)

### Test Coverage Requirements
- Overall coverage: >85%
- LSTM-VAE model: 95%+
- Data generator: 90%+
- Training code: 80%+

---

## SUBMISSION INSTRUCTIONS

### Stage 1: Code Generation (Gemma)
```bash
# At orchestrator "You: " prompt:
@qa-engineer

[Paste this entire task (sections 1-8 above)]
```

### Stage 2: File Creation (Sonnet)
```bash
@integrator

[Paste Stage 1 code output]

Please:
1. Create all files in tests/
2. Run: pytest tests/ -v --cov=src
3. Verify coverage >85%
4. Report results
```

---

## EXPECTED OUTPUT

After completion:
- All tests passing: ✅
- Coverage: >85% ✅
- Test execution time: <30 seconds ✅
- No warnings: ✅

```

---


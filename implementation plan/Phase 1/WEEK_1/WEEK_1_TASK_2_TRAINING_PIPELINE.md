# 📋 TASK 2: Model Training Pipeline
## Tuesday Morning - Stage 1 (Gemma Code Generation)

### **Task Type:** Training Infrastructure
### **Agents:** ML Engineer (primary)
### **Estimated Time:** 15 minutes
### **Success Criteria:** Training script that can train model to 70%+ accuracy

### **Depends On:** Task 1 completion (LSTM-VAE class exists)

---

## **COMPLETE TASK PROMPT FOR STAGE 1**

```
Generate the complete training pipeline for the LSTM-VAE model.
This pipeline trains the model to learn normal greenhouse sensor behavior.

## 1. Training Script

Create src/models/training.py with complete training loop:

### Core Components

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
        checkpoint_dir: str = 'models/checkpoints/',
        log_dir: str = 'logs/tensorboard/'
    ):
        """
        Initialize trainer.
        
        Args:
            model: LSTM-VAE model instance
            train_loader: Training DataLoader
            val_loader: Validation DataLoader
            learning_rate: Adam optimizer learning rate
            weight_decay: L2 regularization
            device: 'cpu' or 'cuda'
            checkpoint_dir: Where to save model checkpoints
            log_dir: TensorBoard log directory
        """
        
    def train_epoch(self, epoch: int) -> Tuple[float, float]:
        """
        Train for one epoch.
        
        Returns:
            (total_loss, reconstruction_loss, kl_loss)
            
        Process:
        1. Iterate through training batches
        2. Forward pass through model
        3. Calculate loss (reconstruction + KL)
        4. Backward pass
        5. Update weights
        6. Log metrics
        """
        
    def validate(self, epoch: int) -> float:
        """
        Validate on validation set.
        
        Returns:
            validation_loss
            
        Process:
        1. No gradient computation
        2. Forward pass
        3. Calculate loss
        4. Log to TensorBoard
        5. Return average loss
        """
        
    def train(
        self,
        epochs: int = 100,
        early_stopping_patience: int = 10,
        checkpoint_interval: int = 5
    ) -> Dict:
        """
        Complete training loop with early stopping.
        
        Args:
            epochs: Total epochs to train
            early_stopping_patience: Stop if val loss doesn't improve for N epochs
            checkpoint_interval: Save checkpoint every N epochs
            
        Returns:
            training_history: {
                'train_loss': [epoch losses],
                'val_loss': [epoch losses],
                'best_epoch': best_epoch,
                'best_val_loss': best_val_loss,
                'training_time': seconds,
                'final_accuracy': accuracy%
            }
            
        Process:
        1. Loop over epochs
        2. Train one epoch
        3. Validate
        4. Track best validation loss
        5. Save checkpoints
        6. Early stopping if no improvement
        7. Return training history
        """
        
    def calculate_accuracy(self, reconstruction_error: float) -> float:
        """
        Calculate accuracy based on reconstruction error.
        
        Accuracy = percentage of samples below error threshold.
        Normal samples should have low reconstruction error.
        
        Args:
            reconstruction_error: MSE between input and reconstruction
            
        Returns:
            accuracy: 0-100%
        """
```

### Training Configuration
- Optimizer: Adam (learning rate 1e-3, weight decay 1e-5)
- Loss: VAE loss (reconstruction + KL divergence)
- Batch size: 32
- Epochs: 100 (with early stopping)
- Validation split: 20%
- Early stopping: 10 epochs without improvement

### Metrics Tracking
- Training loss per epoch
- Validation loss per epoch
- Reconstruction loss
- KL divergence loss
- Validation accuracy
- Training time

---

## 2. TensorBoard Logging

Integrate TensorBoard for visualization:

```python
class TensorBoardLogger:
    def __init__(self, log_dir: str):
        """Initialize TensorBoard writer"""
        
    def log_scalar(self, tag: str, value: float, epoch: int):
        """Log scalar metric (loss, accuracy, etc.)"""
        
    def log_histograms(self, model: LSTMVAE, epoch: int):
        """Log weight distributions of model layers"""
        
    def log_reconstructions(self, original: torch.Tensor, 
                           reconstructed: torch.Tensor, epoch: int):
        """Log sample reconstructions as images"""
        
    def log_latent_space(self, latent_vectors: torch.Tensor, epoch: int):
        """Log 2D projection of latent space"""
```

### Metrics to Log
- `loss/train`: Training loss per batch
- `loss/train_epoch`: Training loss per epoch
- `loss/validation`: Validation loss per epoch
- `loss/reconstruction`: Reconstruction loss
- `loss/kl_divergence`: KL divergence loss
- `accuracy/validation`: Validation accuracy
- `metrics/learning_rate`: Current learning rate
- `model/weights_histogram`: Weight distributions

### TensorBoard Access
```bash
tensorboard --logdir logs/tensorboard/
# View at http://localhost:6006
```

---

## 3. Checkpoint Management

```python
class CheckpointManager:
    def __init__(self, checkpoint_dir: str):
        """Initialize checkpoint manager"""
        
    def save_checkpoint(
        self,
        model: LSTMVAE,
        epoch: int,
        train_loss: float,
        val_loss: float,
        accuracy: float,
        is_best: bool = False
    ) -> str:
        """
        Save model checkpoint with metadata.
        
        Metadata includes:
        - epoch: Which epoch this is from
        - train_loss: Training loss at this epoch
        - val_loss: Validation loss at this epoch
        - accuracy: Validation accuracy
        - timestamp: When checkpoint was saved
        - git_commit: Current git commit (if available)
        - model_config: Model hyperparameters
        - training_config: Training hyperparameters
        
        Files created:
        - checkpoint_epoch_XXX.pt (regular checkpoint)
        - checkpoint_best.pt (best validation loss)
        
        Returns:
            path_to_saved_checkpoint
        """
        
    def load_checkpoint(self, checkpoint_path: str) -> Tuple[LSTMVAE, Dict]:
        """
        Load checkpoint and return model + metadata.
        
        Returns:
            (model, metadata_dict)
        """
        
    def cleanup_old_checkpoints(self, keep_best_n: int = 3):
        """
        Keep only the best N checkpoints to save disk space.
        Always keep best overall checkpoint.
        """
```

### Checkpoint Contents
```python
checkpoint = {
    'epoch': 25,
    'model_state': model.state_dict(),
    'optimizer_state': optimizer.state_dict(),
    'train_loss': 0.0245,
    'val_loss': 0.0289,
    'accuracy': 87.3,
    'timestamp': '2026-05-28T10:30:00Z',
    'model_config': {
        'input_dim': 4,
        'hidden_dims': [64, 32],
        'latent_dim': 8,
        'seq_len': 20,
        'beta': 0.1
    },
    'training_config': {
        'learning_rate': 0.001,
        'batch_size': 32,
        'epochs': 100
    }
}
```

---

## 4. Main Training Script

Create scripts/train_lstm_vae.py as entry point:

```python
#!/usr/bin/env python3
"""
Main training script for LSTM-VAE model.

Usage:
    python scripts/train_lstm_vae.py --epochs 100 --batch_size 32
    
Environment variables:
    DEVICE: 'cpu' or 'cuda' (default: 'cpu')
    LOG_LEVEL: 'DEBUG', 'INFO', 'WARNING' (default: 'INFO')
"""

import argparse
import logging
from pathlib import Path

from src.config import MODEL_CONFIG, TRAINING_CONFIG, DATA_CONFIG
from src.models.lstm_vae import LSTMVAE
from src.models.data_generator import SyntheticDataGenerator
from src.models.data_utils import create_data_loaders
from src.models.training import VAETrainer, CheckpointManager

def main():
    """Main training function"""
    
    parser = argparse.ArgumentParser(description='Train LSTM-VAE model')
    parser.add_argument('--epochs', type=int, default=100, help='Number of epochs')
    parser.add_argument('--batch_size', type=int, default=32, help='Batch size')
    parser.add_argument('--lr', type=float, default=1e-3, help='Learning rate')
    parser.add_argument('--device', type=str, default='cpu', help='cpu or cuda')
    parser.add_argument('--checkpoint_dir', type=str, default='models/checkpoints/')
    parser.add_argument('--log_dir', type=str, default='logs/tensorboard/')
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    
    logger.info("Starting LSTM-VAE training...")
    logger.info(f"Configuration: {MODEL_CONFIG}")
    
    # Generate synthetic data
    logger.info("Generating synthetic greenhouse data...")
    generator = SyntheticDataGenerator(**DATA_CONFIG)
    data = generator.generate_normal_data()
    logger.info(f"Generated data shape: {data.shape}")
    
    # Create data loaders
    train_loader, val_loader = create_data_loaders(
        data,
        batch_size=args.batch_size,
        train_split=0.8
    )
    
    # Initialize model
    logger.info("Initializing LSTM-VAE model...")
    model = LSTMVAE(**MODEL_CONFIG)
    logger.info(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}")
    
    # Initialize trainer
    trainer = VAETrainer(
        model=model,
        train_loader=train_loader,
        val_loader=val_loader,
        learning_rate=args.lr,
        device=args.device,
        checkpoint_dir=args.checkpoint_dir,
        log_dir=args.log_dir
    )
    
    # Train
    logger.info("Starting training...")
    history = trainer.train(
        epochs=args.epochs,
        early_stopping_patience=10,
        checkpoint_interval=5
    )
    
    # Report results
    logger.info("="*50)
    logger.info("Training Complete!")
    logger.info(f"Best epoch: {history['best_epoch']}")
    logger.info(f"Best validation loss: {history['best_val_loss']:.6f}")
    logger.info(f"Final accuracy: {history['final_accuracy']:.2f}%")
    logger.info(f"Training time: {history['training_time']:.1f}s")
    logger.info("="*50)
    
    # Save training history
    import json
    history_path = Path(args.checkpoint_dir) / 'training_history.json'
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, 'w') as f:
        json.dump(history, f, indent=2)
    
    logger.info(f"Training history saved to {history_path}")
    logger.info(f"Best checkpoint: {Path(args.checkpoint_dir) / 'checkpoint_best.pt'}")
    logger.info(f"TensorBoard logs: {args.log_dir}")

if __name__ == '__main__':
    main()
```

---

## 5. Expected Training Behavior

### Typical Training Output
```
Epoch 1/100
  Train Loss: 0.1245 | Val Loss: 0.1089 | Accuracy: 62.3%
Epoch 2/100
  Train Loss: 0.0952 | Val Loss: 0.0945 | Accuracy: 68.5%
...
Epoch 50/100
  Train Loss: 0.0245 | Val Loss: 0.0289 | Accuracy: 87.3%
...
Epoch 95/100
  Train Loss: 0.0198 | Val Loss: 0.0234 | Accuracy: 91.2%

Training Complete!
Best epoch: 92
Best validation loss: 0.0234
Final accuracy: 91.2%
```

### Convergence Expectations
- Loss decreases smoothly (no spikes)
- Validation loss follows training loss closely
- Accuracy reaches 70%+ by epoch 30
- Accuracy reaches 85%+ by epoch 60
- Training time: ~5-10 minutes CPU, ~1-2 minutes GPU

---

## 6. Requirements

### Code Quality
- ✅ Type hints on all functions
- ✅ Comprehensive docstrings
- ✅ Error handling (device placement, NaN detection)
- ✅ Logging at appropriate levels
- ✅ Configuration externalized (no hardcoded values)

### Reproducibility
- ✅ Random seed control
- ✅ Deterministic operations
- ✅ Checkpoint includes all state
- ✅ Training history saved

### Performance
- ✅ Batch processing efficient
- ✅ GPU support (if available)
- ✅ Memory efficient (no data duplication)
- ✅ Early stopping to prevent overfitting

---

## 7. Deliverables for Stage 1

### Code Files (to be created)
- [ ] `src/models/training.py` - VAETrainer class
- [ ] `src/models/checkpoint.py` - CheckpointManager class
- [ ] `scripts/train_lstm_vae.py` - Entry point script

### Documentation
- [ ] Docstrings on all classes and methods
- [ ] Type hints on all functions
- [ ] README with training instructions
- [ ] Example command: `python scripts/train_lstm_vae.py --epochs 100`

### Code Quality
- ✅ All configuration externalized
- ✅ Type hints complete
- ✅ Error handling comprehensive
- ✅ Logging informative

### What NOT to do
- ❌ Do NOT run training (that's integration testing on Wednesday)
- ❌ Do NOT create notebooks yet
- ❌ Do NOT hardcode paths

---

## SUBMISSION INSTRUCTIONS

### Stage 1: Code Generation (Gemma)
```bash
# At orchestrator "You: " prompt:
@ml-engineer

[Paste this entire task (sections 1-7 above)]
```

Output will be:
- Complete VAETrainer implementation
- CheckpointManager class
- Main training script
- All in markdown code blocks with filenames

### Stage 2: File Creation (Sonnet)
After Stage 1 completes, copy the code output and submit:

```bash
@integrator

[Paste Stage 1 code output]

Please:
1. Create all files in src/models/ and scripts/
2. Verify imports work
3. Verify argument parser works: python scripts/train_lstm_vae.py --help
4. Check that TensorBoard logging is configured
5. Report status
```

---

## EXPECTED OUTPUT

After Stage 1, you should have:

```
src/models/
├── training.py (300-400 lines)
└── checkpoint.py (200-250 lines)

scripts/
└── train_lstm_vae.py (150-200 lines)
```

---

## SUCCESS CRITERIA

✅ Training script runs without errors
✅ Can import all dependencies
✅ Argument parser works
✅ Checkpoints save with metadata
✅ TensorBoard logging configured
✅ Training loop is complete
✅ Early stopping implemented
✅ All docstrings present
✅ All type hints present

```

---


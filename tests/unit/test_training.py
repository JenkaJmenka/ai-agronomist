import pytest
from unittest.mock import MagicMock, patch
from src.models.training import VAETrainer
from src.models.lstm_vae import LSTMVAE
import torch
from torch.utils.data import DataLoader, TensorDataset

@pytest.fixture
def mock_model():
    """Fixture for a mocked LSTMVAE model."""
    model = MagicMock(spec=LSTMVAE)
    model.parameters.return_value = [torch.nn.Parameter(torch.randn(10, 10))]
    model.loss_function.return_value = torch.tensor(0.5, requires_grad=True)
    model.return_value = (torch.randn(5, 20, 4), torch.randn(5, 16), torch.randn(5, 16), torch.randn(5, 20, 16))
    model.to.return_value = model
    return model

@pytest.fixture
def mock_data_loader():
    """Fixture for a mocked DataLoader."""
    dataset = TensorDataset(torch.randn(10, 20, 4))
    return DataLoader(dataset, batch_size=5)

@pytest.fixture
def trainer_config():
    """Fixture for trainer configuration."""
    return {
        "learning_rate": 1e-3,
        "weight_decay": 1e-5,
        "early_stopping_patience": 3,
        "epochs": 10,
    }

@pytest.fixture
def trainer(mock_model, mock_data_loader, trainer_config, tmp_path):
    """Fixture for a VAETrainer instance."""
    return VAETrainer(
        model=mock_model,
        train_loader=mock_data_loader,
        val_loader=mock_data_loader,
        config=trainer_config,
        checkpoint_dir=str(tmp_path / "checkpoints"),
        log_dir=str(tmp_path / "logs"),
        device=torch.device("cpu"),
    )

def test_trainer_instantiation(trainer):
    """Test if the VAETrainer is instantiated correctly."""
    assert trainer is not None
    assert isinstance(trainer, VAETrainer)

def test_train_epoch(trainer, mock_model):
    """Test a single training epoch."""
    epoch = 1
    loss = trainer.train_epoch(epoch)
    assert isinstance(loss, float)
    assert loss > 0
    assert mock_model.loss_function.called

def test_validate(trainer, mock_model):
    """Test a single validation run."""
    epoch = 1
    loss = trainer.validate(epoch)
    assert isinstance(loss, float)
    assert loss > 0
    assert mock_model.loss_function.called

@patch('src.models.training.VAETrainer.validate', return_value=0.1)
@patch('src.models.training.VAETrainer.train_epoch', return_value=0.2)
def test_train_loop_and_early_stopping(mock_train_epoch, mock_validate, trainer):
    """Test the main training loop and early stopping."""
    # Test early stopping
    mock_validate.side_effect = [0.5, 0.6, 0.7, 0.8]
    history = trainer.train()
    assert len(history["epochs"]) == 4  # 1 initial + 3 patience
    assert history["best_val_loss"] == 0.5
    assert history["best_epoch"] == 1

def test_checkpoint_saving(trainer):
    """Test if checkpoints are saved."""
    with patch.object(trainer.model, 'save_checkpoint') as mock_save:
        trainer.train()
        assert mock_save.called

#!/usr/bin/env python3
"""CLI entry point for training the LSTM-VAE model on synthetic greenhouse data."""

import argparse
import os
import sys
import argparse
import logging
import sys
import os
from pathlib import Path

# Add the parent directory to sys.path so 'src' can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import numpy as np
import torch

from src import config
from src.models.data_generator import SyntheticDataGenerator
from src.models.data_utils import create_data_loaders
from src.models.lstm_vae import LSTMVAE
from src.models.training import VAETrainer


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments, defaulting to values from src.config."""
    parser = argparse.ArgumentParser(
        description="Train the LSTM-VAE anomaly detection model on synthetic greenhouse data."
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=config.TRAINING_CONFIG["epochs"],
        help="Number of training epochs.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=config.TRAINING_CONFIG["batch_size"],
        dest="batch_size",
        help="Mini-batch size.",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=config.TRAINING_CONFIG["learning_rate"],
        help="Adam learning rate.",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=str,
        default=config.PATHS["checkpoint_dir"],
        dest="checkpoint_dir",
        help="Directory to save per-epoch checkpoints.",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=config.PATHS["log_dir"],
        dest="log_dir",
        help="TensorBoard log directory.",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="auto",
        help="Device: 'auto' | 'cpu' | 'cuda' | 'mps'.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=config.RANDOM_SEED,
        help="Random seed for reproducibility.",
    )
    return parser.parse_args()


def detect_device(device_arg: str) -> torch.device:
    """Resolve device string to a torch.device.

    Args:
        device_arg: 'auto' to pick best available, or an explicit device string.

    Returns:
        Resolved torch.device.
    """
    if device_arg != "auto":
        return torch.device(device_arg)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def main() -> None:
    """Generate data, build model, run training, and report results."""
    args = parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    device = detect_device(args.device)
    print(f"Device: {device}")

    print("Generating synthetic data...")
    generator = SyntheticDataGenerator(
        num_samples=config.DATA_CONFIG["num_sequences"],
        seq_len=config.DATA_CONFIG["seq_len"],
        input_dim=config.DATA_CONFIG["input_dim"],
        seed=args.seed,
    )
    tensor_data, _ = generator.generate()

    validation_split: float = config.TRAINING_CONFIG["validation_split"]
    train_loader, val_loader = create_data_loaders(
        tensor_data.numpy(),
        batch_size=args.batch_size,
        train_split=1.0 - validation_split,
    )
    print(f"Train batches: {len(train_loader)}  Val batches: {len(val_loader)}")

    model = LSTMVAE(**config.MODEL_CONFIG).to(device)
    print(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}")

    training_cfg = {
        **config.TRAINING_CONFIG,
        "learning_rate": args.lr,
        "batch_size": args.batch_size,
    }

    os.makedirs(args.checkpoint_dir, exist_ok=True)
    os.makedirs(args.log_dir, exist_ok=True)

    trainer = VAETrainer(
        model=model,
        train_loader=train_loader,
        val_loader=val_loader,
        config=training_cfg,
        checkpoint_dir=args.checkpoint_dir,
        log_dir=args.log_dir,
        device=device,
    )

    try:
        history = trainer.train()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    print(f"\nTraining complete.")
    print(f"  Epochs trained : {len(history['epochs'])}")
    print(f"  Final val loss : {history['val_loss'][-1]:.4f}")
    print(f"  Final accuracy : {history['final_accuracy']:.4f}")

    final_ckpt = os.path.join(
        args.checkpoint_dir, f"epoch_{history['epochs'][-1]:04d}.pt"
    )
    print(f"  Checkpoint     : {final_ckpt}")


if __name__ == "__main__":
    main()

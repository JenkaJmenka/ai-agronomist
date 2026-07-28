import os
import torch
import torch.nn as nn
from typing import Dict, Any, Optional
from datetime import datetime
import numpy as np

from src.models.lstm_vae import LSTMVAE
from src.config import CUSTOMER_MODEL_DIR, DEVICE, SEQ_LEN

class TransferLearner:
    """
    Handles fine-tuning a pre-trained baseline model (Week 1) on customer-specific data.
    Freezes the encoder and only fine-tunes the decoder for efficiency and to prevent catastrophic forgetting.
    """
    
    def __init__(self, checkpoint_path: str, device: str = DEVICE):
        self.device = torch.device(device)
        self.checkpoint_path = checkpoint_path
        self.model = LSTMVAE().to(self.device)
        self.checkpoint = self.model.load_checkpoint(self.checkpoint_path)
        
    def _freeze_encoder(self):
        """Freeze all encoder weights"""
        for param in self.model.encoder_layers.parameters():
            param.requires_grad = False
        for param in self.model.fc_mu.parameters():
            param.requires_grad = False
        for param in self.model.fc_logvar.parameters():
            param.requires_grad = False
            
    def _unfreeze_decoder(self):
        """Unfreeze decoder weights for training"""
        for param in self.model.decoder_layers.parameters():
            param.requires_grad = True
        for param in self.model.fc_out.parameters():
            param.requires_grad = True

    async def fine_tune(self, 
                        customer_id: str,
                        customer_data: np.ndarray,
                        epochs: int = 10,
                        learning_rate: float = 0.0001,
                        batch_size: int = 32) -> str:
        """
        Fine-tune Week 1 model on customer data.
        """
        self._freeze_encoder()
        self._unfreeze_decoder()
        
        optimizer = torch.optim.Adam(
            filter(lambda p: p.requires_grad, self.model.parameters()), 
            lr=learning_rate
        )
        
        # Prepare sequences
        sequences = []
        for i in range(len(customer_data) - SEQ_LEN):
            sequences.append(customer_data[i:i+SEQ_LEN])
        
        if not sequences:
            raise ValueError("Not enough customer data to create sequences")
            
        sequences = torch.tensor(np.array(sequences), dtype=torch.float32)
        dataset = torch.utils.data.TensorDataset(sequences)
        
        # Split into train/test
        train_size = int(0.8 * len(dataset))
        test_size = len(dataset) - train_size
        train_dataset, test_dataset = torch.utils.data.random_split(dataset, [train_size, test_size])
        
        train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=batch_size)
        
        best_loss = float('inf')
        save_path = os.path.join(CUSTOMER_MODEL_DIR, f"{customer_id}_model.pt")
        
        for epoch in range(epochs):
            self.model.train()
            train_loss = 0.0
            
            for batch in train_loader:
                x = batch[0].to(self.device)
                optimizer.zero_grad()
                
                recon_x, mu, logvar, _ = self.model(x)
                loss = self.model.loss_function(recon_x, x, mu, logvar)
                
                loss.backward()
                optimizer.step()
                train_loss += loss.item()
                
            train_loss /= len(train_loader)
            
            # Evaluate
            self.model.eval()
            test_loss = 0.0
            with torch.no_grad():
                for batch in test_loader:
                    x = batch[0].to(self.device)
                    recon_x, mu, logvar, _ = self.model(x)
                    loss = self.model.loss_function(recon_x, x, mu, logvar)
                    test_loss += loss.item()
                    
            test_loss /= len(test_loader)
            
            if test_loss < best_loss:
                best_loss = test_loss
                # We save with accuracy = 0.0 because it's unsupervised
                self.model.save_checkpoint(save_path, epoch, test_loss, 0.0)
                
        return save_path

    async def fine_tune_portions(
        self,
        customer_id: str,
        portion_factory,
        *,
        epochs: int = 5,
        learning_rate: float = 0.0001,
        batch_size: int = 32,
        progress_cb=None,
    ) -> str:
        """Fine-tune from a stream of data portions instead of one big array.

        `portion_factory` returns a fresh async iterator of (N, 4) arrays per
        epoch (e.g. a DB keyset pager), so peak memory is one portion. A
        SEQ_LEN-row carry between portions keeps boundary sequences intact.
        `progress_cb(epoch, portion_idx, avg_loss)` (async) reports progress.
        """
        self._freeze_encoder()
        self._unfreeze_decoder()

        optimizer = torch.optim.Adam(
            filter(lambda p: p.requires_grad, self.model.parameters()),
            lr=learning_rate,
        )

        best_loss = float("inf")
        save_path = os.path.join(CUSTOMER_MODEL_DIR, f"{customer_id}_model.pt")

        for epoch in range(epochs):
            self.model.train()
            epoch_loss = 0.0
            batch_count = 0
            carry: Optional[np.ndarray] = None
            portion_idx = 0

            async for portion in portion_factory():
                data = portion if carry is None else np.concatenate([carry, portion])
                if len(data) > SEQ_LEN:
                    windows = np.stack(
                        [data[i : i + SEQ_LEN] for i in range(len(data) - SEQ_LEN)]
                    )
                    tensor = torch.tensor(windows, dtype=torch.float32)
                    loader = torch.utils.data.DataLoader(
                        torch.utils.data.TensorDataset(tensor),
                        batch_size=batch_size,
                        shuffle=True,
                    )
                    for batch in loader:
                        x = batch[0].to(self.device)
                        optimizer.zero_grad()
                        recon_x, mu, logvar, _ = self.model(x)
                        loss = self.model.loss_function(recon_x, x, mu, logvar)
                        loss.backward()
                        optimizer.step()
                        epoch_loss += loss.item()
                        batch_count += 1
                carry = data[-SEQ_LEN:] if len(data) >= SEQ_LEN else data
                portion_idx += 1
                if progress_cb is not None:
                    await progress_cb(
                        epoch, portion_idx, epoch_loss / max(batch_count, 1)
                    )

            if batch_count == 0:
                raise ValueError("Not enough customer data to create sequences")

            avg_loss = epoch_loss / batch_count
            if avg_loss < best_loss:
                best_loss = avg_loss
                # accuracy=0.0 — unsupervised objective
                self.model.save_checkpoint(save_path, epoch, avg_loss, 0.0)

        return save_path

import os
import torch
import torch.nn as nn
from typing import Dict, Any, Optional
from datetime import datetime
import numpy as np

from src.models.lstm_vae import LSTMVAE
from src.config import DEVICE, SEQ_LEN

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
        save_path = f"models/{customer_id}_model.pt"
        
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

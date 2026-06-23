import torch
import torch.nn.functional as F
import numpy as np
from typing import Dict, Any
from src.models.lstm_vae import LSTMVAE
from src.config import DEVICE, SEQ_LEN

class ModelEvaluator:
    """
    Evaluates LSTM-VAE model performance and compares baseline vs fine-tuned models.
    """
    def __init__(self, device: str = DEVICE):
        self.device = torch.device(device)
        
    async def evaluate(self, model: LSTMVAE, test_data: np.ndarray) -> Dict[str, Any]:
        """
        Evaluate model performance on test_data.
        test_data: array of shape (N, 4)
        """
        model.eval()
        
        # Prepare sequences
        sequences = []
        for i in range(len(test_data) - SEQ_LEN):
            sequences.append(test_data[i:i+SEQ_LEN])
            
        if not sequences:
            raise ValueError("Not enough test data to create sequences")
            
        sequences = torch.tensor(np.array(sequences), dtype=torch.float32).to(self.device)
        
        with torch.no_grad():
            recon_x, _, _, _ = model(sequences)
            mse_loss = F.mse_loss(recon_x, sequences, reduction="none")
            # Mean error per sequence (across sequence length and features)
            seq_errors = mse_loss.mean(dim=[1, 2]).cpu().numpy()
            
            # Global MSE
            global_mse = float(mse_loss.mean().item())
            
        return {
            "mse": global_mse,
            "reconstruction_error_mean": float(np.mean(seq_errors)),
            "reconstruction_error_std": float(np.std(seq_errors)),
            "anomaly_score_distribution": {
                "min": float(np.min(seq_errors)),
                "max": float(np.max(seq_errors)),
                "mean": float(np.mean(seq_errors)),
                "percentiles": {
                    "25": float(np.percentile(seq_errors, 25)),
                    "50": float(np.percentile(seq_errors, 50)),
                    "75": float(np.percentile(seq_errors, 75))
                }
            }
        }
        
    def compare(self, baseline_eval: Dict[str, Any], customer_eval: Dict[str, Any]) -> Dict[str, Any]:
        """
        Compare Week 1 vs adapted model.
        Returns improvement metrics.
        """
        baseline_err = baseline_eval["reconstruction_error_mean"]
        customer_err = customer_eval["reconstruction_error_mean"]
        
        improvement = 0.0
        if baseline_err > 0:
            improvement = ((baseline_err - customer_err) / baseline_err) * 100.0
            
        return {
            "improvement_percent": float(improvement),
            "baseline_error": float(baseline_err),
            "customer_error": float(customer_err)
        }

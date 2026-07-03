import re

with open("src/models/inference.py", "r") as f:
    content = f.read()

# Add MAX_OBSERVE_BATCH to imports
content = content.replace("from src.config import DEVICE", "from src.config import DEVICE, MAX_OBSERVE_BATCH")

# Replace predict_batch
old_predict_batch = """    def predict_batch(self, sequences: List[np.ndarray]) -> np.ndarray:
        # Convert list of sequence chunks into proper batched tensor array
        # assuming each seq is (seq_len, input_dim) -> stack into (batch, seq_len, input_dim)
        stacked = np.stack(sequences)
        return self.predict(stacked)"""

new_predict_batch = """    def predict_batch(self, sequences: List[np.ndarray]) -> np.ndarray:
        stacked = np.stack(sequences)
        self._validate_batch(stacked)
        
        all_scores = []
        with torch.no_grad():
            for i in range(0, len(stacked), MAX_OBSERVE_BATCH):
                batch = stacked[i:i + MAX_OBSERVE_BATCH]
                batch_tensor = torch.from_numpy(batch).float()
                reconstructed, _, _, _ = self.model(batch_tensor)
                scores = torch.mean((batch_tensor - reconstructed) ** 2, dim=[1, 2])
                all_scores.append(scores.numpy())
                
        return np.concatenate(all_scores) if all_scores else np.array([])"""

content = content.replace(old_predict_batch, new_predict_batch)

# Replace predict_batch_enriched
old_predict_batch_enriched = """        stacked = np.stack(sequences) if isinstance(sequences, list) else sequences
        self._validate_batch(stacked)

        with torch.no_grad():
            sequences_tensor = torch.from_numpy(stacked).float()
            reconstructed_sequences, mu, logvar, _ = self.model(sequences_tensor)
            # MSE across feature & seq dimensions (same scoring contract as predict)
            scores = torch.mean((sequences_tensor - reconstructed_sequences) ** 2, dim=[1, 2])
            # std = exp(0.5*logvar) — unified uncertainty convention (see docstring)
            uncertainties = torch.exp(0.5 * logvar).mean(dim=1)

        return scores.numpy(), mu.numpy(), uncertainties.numpy()"""

new_predict_batch_enriched = """        stacked = np.stack(sequences) if isinstance(sequences, list) else sequences
        self._validate_batch(stacked)

        all_scores = []
        all_mu = []
        all_uncertainties = []
        
        with torch.no_grad():
            for i in range(0, len(stacked), MAX_OBSERVE_BATCH):
                batch = stacked[i:i + MAX_OBSERVE_BATCH]
                batch_tensor = torch.from_numpy(batch).float()
                reconstructed, mu, logvar, _ = self.model(batch_tensor)
                
                scores = torch.mean((batch_tensor - reconstructed) ** 2, dim=[1, 2])
                uncertainties = torch.exp(0.5 * logvar).mean(dim=1)
                
                all_scores.append(scores.numpy())
                all_mu.append(mu.numpy())
                all_uncertainties.append(uncertainties.numpy())

        if not all_scores:
            return np.array([]), np.array([]), np.array([])
            
        return (
            np.concatenate(all_scores),
            np.concatenate(all_mu),
            np.concatenate(all_uncertainties)
        )"""

content = content.replace(old_predict_batch_enriched, new_predict_batch_enriched)

with open("src/models/inference.py", "w") as f:
    f.write(content)

print("Updated inference.py")

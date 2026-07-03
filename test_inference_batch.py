import numpy as np
import torch
from src.models.inference import AnomalyDetector
detector = AnomalyDetector()
sequences = [np.random.rand(20, 4).astype(np.float32) for _ in range(5000)]
print("Running 5000 batch inference...")
detector.predict_batch_enriched(sequences)
print("Success")

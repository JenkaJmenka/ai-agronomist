import numpy as np
from typing import Dict, Any

class BaselineAnalyzer:
    """
    Analyzes customer greenhouse data to extract baseline patterns including
    normal ranges, correlations, and drift.
    """
    
    def __init__(self):
        self.sensors = ["temperature", "humidity", "co2", "light"]
        
    def analyze(self, customer_data: np.ndarray) -> Dict[str, Any]:
        """
        Analyze customer normal operation data.
        
        Input: customer_data array of shape (N, 4) representing [temp, humidity, co2, light]
        Output: Dictionary containing ranges, correlations, and drift.
        """
        if customer_data.shape[1] != 4:
            raise ValueError(f"Expected customer_data to have 4 features, got {customer_data.shape[1]}")
            
        results = {
            "ranges": {},
            "correlations": {},
            "drift": {}
        }
        
        for idx, sensor in enumerate(self.sensors):
            results["ranges"][sensor] = {
                "min": float(np.min(customer_data[:, idx])),
                "max": float(np.max(customer_data[:, idx])),
                "mean": float(np.mean(customer_data[:, idx])),
                "std": float(np.std(customer_data[:, idx]))
            }
            
        # Correlations
        corr_matrix = np.corrcoef(customer_data.T)
        results["correlations"]["temp_humidity"] = float(corr_matrix[0, 1])
        results["correlations"]["humidity_light"] = float(corr_matrix[1, 3])
        
        # Drift detection via linear trend
        x = np.arange(len(customer_data))
        for idx, sensor in enumerate(self.sensors):
            # Use polyfit to get slope
            slope, _ = np.polyfit(x, customer_data[:, idx], 1)
            # Define a threshold for "stable" vs "increasing/decreasing"
            # Normalize slope by mean to get percentage change per interval
            mean_val = results["ranges"][sensor]["mean"]
            norm_slope = slope / (mean_val + 1e-6)
            
            if norm_slope > 1e-4:
                drift_status = "increasing"
            elif norm_slope < -1e-4:
                drift_status = "decreasing"
            else:
                drift_status = "stable"
                
            results["drift"][sensor] = drift_status
            
        return results
        
    def save_baseline(self, customer_id: str, baseline: Dict[str, Any]) -> str:
        """
        In a real scenario, this would save the baseline dict to SQLite.
        For now, returns a simulated ID.
        """
        return f"baseline_{customer_id}_v1"

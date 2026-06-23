import asyncio
import numpy as np
import logging
from typing import Dict, Tuple, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
import glob

from src.models.transfer_learner import TransferLearner
from src.models.model_evaluator import ModelEvaluator
from src.models.model_registry import ModelRegistry
from src.models.lstm_vae import LSTMVAE
from src.db.models import SensorReading

logger = logging.getLogger(__name__)

class PerCustomerTrainingPipeline:
    """
    End-to-end pipeline to train, evaluate, and register a customer-specific model.
    """
    
    def __init__(self, db_session: AsyncSession, baseline_checkpoint_path: Optional[str] = None):
        self.db = db_session
        self.registry = ModelRegistry(self.db)
        
        if not baseline_checkpoint_path:
            # Fallback to the latest checkpoint
            checkpoints = sorted(glob.glob("models/checkpoints/epoch_*.pt"))
            self.baseline_checkpoint_path = checkpoints[-1] if checkpoints else "models/baseline.pt"
        else:
            self.baseline_checkpoint_path = baseline_checkpoint_path
            
    async def _load_customer_data(self, customer_id: str) -> np.ndarray:
        """Loads all sequences for a customer from the database"""
        stmt = select(SensorReading).where(
            SensorReading.tenant_id == customer_id
        ).order_by(SensorReading.timestamp.asc())
        
        result = await self.db.execute(stmt)
        readings = result.scalars().all()
        
        data = []
        for r in readings:
            data.append([r.temperature, r.humidity, r.co2, r.light])
            
        return np.array(data)

    async def train_customer_model(self, customer_id: str) -> Optional[str]:
        """
        Main entrypoint. Trains a customer-specific model asynchronously.
        Returns the new model_id if registered, else None.
        """
        logger.info(f"Starting training pipeline for {customer_id}")
        
        # 1. Load Data
        customer_data = await self._load_customer_data(customer_id)
        if len(customer_data) < 100:
            logger.warning(f"Not enough data to train model for {customer_id}")
            return None
            
        # 2. Transfer Learning
        learner = TransferLearner(checkpoint_path=self.baseline_checkpoint_path)
        try:
            new_checkpoint = await learner.fine_tune(customer_id, customer_data, epochs=5)
        except Exception as e:
            logger.error(f"Transfer learning failed: {e}")
            return None
            
        # 3. Evaluation
        evaluator = ModelEvaluator()
        
        # Evaluate New Model
        new_model = LSTMVAE()
        new_model.load_checkpoint(new_checkpoint)
        new_metrics = await evaluator.evaluate(new_model, customer_data)
        
        # Evaluate Baseline Model
        baseline_model = LSTMVAE()
        baseline_model.load_checkpoint(self.baseline_checkpoint_path)
        base_metrics = await evaluator.evaluate(baseline_model, customer_data)
        
        # Compare
        comp = evaluator.compare(base_metrics, new_metrics)
        logger.info(f"Training completed. Improvement: {comp['improvement_percent']:.2f}%")
        
        # 4. Register if better (or if there's no active model)
        active_model = await self.registry.get_active_model(customer_id)
        
        if not active_model or comp['improvement_percent'] > 0:
            # Register it
            model_id = await self.registry.register_model(
                customer_id=customer_id,
                model_type="transfer_learned",
                checkpoint_path=new_checkpoint,
                baseline_id=None,
                metrics=comp
            )
            logger.info(f"Registered new model {model_id} for {customer_id}")
            return model_id
            
        logger.info(f"New model did not improve baseline for {customer_id}. Discarding.")
        return None

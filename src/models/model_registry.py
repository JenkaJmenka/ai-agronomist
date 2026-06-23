import json
import uuid
from typing import Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from src.db.models import ModelVersion, ModelDeployment, ModelBaseline

class ModelRegistry:
    """
    Manages the lifecycle, versioning, and deployment tracking of ML models using AsyncSession.
    """
    
    def __init__(self, db_session: AsyncSession):
        self.db = db_session
        
    async def register_model(self,
                       customer_id: str,
                       model_type: str,
                       checkpoint_path: str,
                       baseline_id: Optional[str] = None,
                       metrics: Optional[Dict] = None) -> str:
        """
        Registers a new model version and sets it as the active deployment for the customer.
        Returns the new model_id.
        """
        model_id = f"model_{uuid.uuid4().hex[:8]}"
        
        # Create model version
        version = ModelVersion(
            id=model_id,
            customer_id=customer_id,
            model_type=model_type,
            checkpoint_path=checkpoint_path,
            baseline_id=baseline_id,
            metrics_json=json.dumps(metrics) if metrics else None,
            status="active"
        )
        self.db.add(version)
        
        # Deactivate previous deployments
        stmt = select(ModelDeployment).where(
            ModelDeployment.customer_id == customer_id,
            ModelDeployment.is_current == True
        )
        result = await self.db.execute(stmt)
        old_deployments = result.scalars().all()
        
        for d in old_deployments:
            d.is_current = False
            
        # Create new deployment
        deployment = ModelDeployment(
            id=f"dep_{uuid.uuid4().hex[:8]}",
            customer_id=customer_id,
            model_id=model_id,
            is_current=True
        )
        self.db.add(deployment)
        
        await self.db.commit()
        return model_id
        
    async def get_active_model(self, customer_id: str) -> Optional[Dict]:
        """Gets the currently deployed model for a customer."""
        stmt = select(ModelDeployment).where(
            ModelDeployment.customer_id == customer_id,
            ModelDeployment.is_current == True
        )
        result = await self.db.execute(stmt)
        deployment = result.scalars().first()
        
        if not deployment:
            return None
            
        stmt2 = select(ModelVersion).where(ModelVersion.id == deployment.model_id)
        result2 = await self.db.execute(stmt2)
        version = result2.scalars().first()
        
        if not version:
            return None
            
        return {
            "model_id": version.id,
            "customer_id": version.customer_id,
            "model_type": version.model_type,
            "checkpoint_path": version.checkpoint_path,
            "metrics": json.loads(version.metrics_json) if version.metrics_json else {},
            "created_at": version.created_at.isoformat() if version.created_at else None
        }
        
    async def list_models(self, customer_id: str) -> List[Dict]:
        """Lists all model versions for a customer."""
        stmt = select(ModelVersion).where(ModelVersion.customer_id == customer_id).order_by(ModelVersion.created_at.desc())
        result = await self.db.execute(stmt)
        versions = result.scalars().all()
        
        output = []
        for v in versions:
            stmt2 = select(ModelDeployment).where(
                ModelDeployment.model_id == v.id,
                ModelDeployment.is_current == True
            )
            r2 = await self.db.execute(stmt2)
            is_active = r2.scalars().first() is not None
            
            output.append({
                "model_id": v.id,
                "model_type": v.model_type,
                "metrics": json.loads(v.metrics_json) if v.metrics_json else {},
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "status": v.status,
                "is_active": is_active
            })
            
        return output
        
    async def rollback_model(self, customer_id: str, model_id: str) -> bool:
        """Rolls back to a specific previous model version."""
        stmt = select(ModelVersion).where(
            ModelVersion.id == model_id,
            ModelVersion.customer_id == customer_id
        )
        result = await self.db.execute(stmt)
        version = result.scalars().first()
        
        if not version:
            return False
            
        # Deactivate current
        stmt_deps = select(ModelDeployment).where(
            ModelDeployment.customer_id == customer_id,
            ModelDeployment.is_current == True
        )
        res_deps = await self.db.execute(stmt_deps)
        old_deployments = res_deps.scalars().all()
        
        for d in old_deployments:
            d.is_current = False
            
        # Deploy requested model
        deployment = ModelDeployment(
            id=f"dep_{uuid.uuid4().hex[:8]}",
            customer_id=customer_id,
            model_id=model_id,
            is_current=True
        )
        self.db.add(deployment)
        await self.db.commit()
        return True
        
    async def archive_model(self, model_id: str) -> bool:
        """Marks a model as archived."""
        stmt = select(ModelVersion).where(ModelVersion.id == model_id)
        result = await self.db.execute(stmt)
        version = result.scalars().first()
        
        if not version:
            return False
            
        version.status = "archived"
        await self.db.commit()
        return True

"""Seasonal baseline endpoints — I/O only; logic in the service layer.

GET /api/baselines/seasonal?tenant_id=&greenhouse_id=
  → global + summer/winter mean/std/median + σ_season/σ_global factors
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.services.seasonal_baseline_service import compute_seasonal_baselines
from src.db.session import get_db
from src.models.operational_config_store import get_operational_config_store
from src.models.seasonal_scaler import build_scaler_from_baselines

router = APIRouter(prefix="/api/baselines", tags=["Seasonal Baselines"])


@router.get("/seasonal")
async def get_seasonal_baselines(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query("default"),
    auth_tenant: str | None = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return seasonal baselines plus detector scale factors."""
    enforce_tenant_access(auth_tenant, tenant_id)
    store = get_operational_config_store()
    config = await store.load_from_db(tenant_id, greenhouse_id, db)
    baselines = await compute_seasonal_baselines(
        db,
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        season_map=config.get("season_map"),
    )
    scaler = build_scaler_from_baselines(
        baselines,
        overrides=config.get("seasonal_overrides"),
    )
    return {
        **baselines,
        "factors": {
            "summer": scaler.factors_for_season("summer"),
            "winter": scaler.factors_for_season("winter"),
        },
        "crop": config.get("crop"),
    }

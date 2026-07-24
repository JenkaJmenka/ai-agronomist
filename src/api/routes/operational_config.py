"""Operational config endpoints — I/O only; all logic in the service layer.

Routes
------
GET  /api/config/operational
     Query params: tenant_id (required), greenhouse_id (default: 'default').
     Returns: merged config + per-top-level-key source annotation.

PUT  /api/config/operational
     Query params: tenant_id (required), greenhouse_id (default: 'default').
     Body: full OperationalConfigRequest document (Pydantic v2).
     Returns: merged config after successful upsert.

GET  /api/config/crop-profiles
     Returns: static CROP_SETPOINT_PROFILES dict (no auth required).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import enforce_tenant_access, require_api_key
from src.api.services.operational_config_service import upsert_operational_config
from src.config import CROP_SETPOINT_PROFILES
from src.db.session import get_db
from src.models.operational_config_store import get_operational_config_store

router = APIRouter(prefix="/api/config", tags=["Operational Config"])

# ---------------------------------------------------------------------------
# Pydantic v2 request / response schemas
# ---------------------------------------------------------------------------


class SetpointBand(BaseModel):
    """Day/night setpoint band for a single sensor channel.

    ``night`` may be ``null`` to signal "use the day band as fallback".
    """

    day: list[float] | None = Field(None, description="[lo, hi] day band")
    night: list[float] | None = Field(None, description="[lo, hi] night band, or null")


class DayWindow(BaseModel):
    start_hour: int = Field(6, ge=0, le=23)
    end_hour: int = Field(22, ge=0, le=23)


class SeasonalOverride(BaseModel):
    detector_scale: float | None = Field(None)


class OperationalConfigRequest(BaseModel):
    """Full operational config document (PUT body)."""

    crop: str
    setpoints: dict[str, SetpointBand] = Field(default_factory=dict)
    day_window: DayWindow = Field(default_factory=DayWindow)
    season_map: dict[str, Literal["summer", "winter"]] = Field(default_factory=dict)
    seasonal_overrides: dict[str, SeasonalOverride] = Field(default_factory=dict)


class OperationalConfigResponse(BaseModel):
    """GET response: merged config + per-key provenance annotation."""

    config: dict[str, Any]
    sources: dict[str, Literal["default", "override"]]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/operational", response_model=OperationalConfigResponse)
async def get_operational_config(
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query("default"),
    auth_tenant: str | None = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> OperationalConfigResponse:
    """Return the merged operational config plus per-key source annotation."""
    enforce_tenant_access(auth_tenant, tenant_id)
    store = get_operational_config_store()
    merged = await store.load_from_db(tenant_id, greenhouse_id, db)
    raw = store.get_raw_overrides(tenant_id, greenhouse_id)
    sources: dict[str, Literal["default", "override"]] = {
        key: ("override" if key in raw else "default")
        for key in merged
    }
    return OperationalConfigResponse(config=merged, sources=sources)


@router.put("/operational", response_model=OperationalConfigResponse)
async def put_operational_config(
    body: OperationalConfigRequest,
    tenant_id: str = Query(..., min_length=1),
    greenhouse_id: str = Query("default"),
    auth_tenant: str | None = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
) -> OperationalConfigResponse:
    """Validate and upsert the operational config for (tenant, greenhouse)."""
    enforce_tenant_access(auth_tenant, tenant_id)
    store = get_operational_config_store()
    config_dict = body.model_dump(mode="python")
    merged = await upsert_operational_config(
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        config=config_dict,
        store=store,
        session=db,
    )
    raw = store.get_raw_overrides(tenant_id, greenhouse_id)
    sources: dict[str, Literal["default", "override"]] = {
        key: ("override" if key in raw else "default")
        for key in merged
    }
    return OperationalConfigResponse(config=merged, sources=sources)


@router.get("/crop-profiles")
async def get_crop_profiles() -> dict[str, Any]:
    """Return the static crop setpoint profile templates (no auth required)."""
    return CROP_SETPOINT_PROFILES

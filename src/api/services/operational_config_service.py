"""Validation, upsert, and post-mutation cache fan-out for operational config.

Validation rules
----------------
- ``crop``: must be a key of CROP_SETPOINT_PROFILES.
- ``setpoints``: every ``[min, max]`` band must satisfy min < max;
  ``null`` is allowed (means "use day band as fallback").
- ``day_window``: ``start_hour`` and ``end_hour`` each 0–23; must differ.
- ``season_map``: must cover all 12 calendar months ("1".."12") with values
  in {``summer``, ``winter``}.

Post-mutation fan-out (called after every successful upsert)
------------------------------------------------------------
1. ``invalidate_response_cache('operational_config')`` — clears the FastAPICache
   Redis backend so stale GET responses are not served after a config change.
2. ``invalidate_seasonal_caches(tenant_id)`` — no-op placeholder until Phase 4.
   TODO(scada-p4): wire this to SeasonalScaleProvider and CropBoundsDetector
   to flush cached per-season sigma multipliers and setpoint bands when the
   tenant changes their operational config mid-season.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.services.cache_invalidation import invalidate_response_cache
from src.config import CROP_SETPOINT_PROFILES
from src.models.operational_config_store import OperationalConfigStore

logger = logging.getLogger(__name__)

_VALID_SEASONS = frozenset({"summer", "winter"})
_ALL_MONTHS = frozenset(str(m) for m in range(1, 13))


def _validate_band(channel: str, period: str, band: list[Any] | None) -> None:
    """Raise 422 if a setpoint band is present but structurally invalid."""
    if band is None:
        return  # null → day-band fallback; valid
    if not (isinstance(band, list) and len(band) == 2):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"setpoints.{channel}.{period} must be [min, max] or null",
        )
    lo, hi = band
    if not (isinstance(lo, (int, float)) and isinstance(hi, (int, float))):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"setpoints.{channel}.{period} values must be numeric",
        )
    if lo >= hi:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"setpoints.{channel}.{period}: min ({lo}) must be strictly"
                f" less than max ({hi})"
            ),
        )


def _validate_setpoints(setpoints: dict[str, Any]) -> None:
    for channel, periods in setpoints.items():
        if not isinstance(periods, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"setpoints.{channel} must be a dict with 'day'/'night' keys",
            )
        _validate_band(channel, "day", periods.get("day"))
        _validate_band(channel, "night", periods.get("night"))


def _validate_season_map(season_map: dict[str, Any]) -> None:
    received = frozenset(str(k) for k in season_map.keys())
    missing = _ALL_MONTHS - received
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"season_map is missing months: {sorted(missing, key=int)}",
        )
    for month, season in season_map.items():
        if season not in _VALID_SEASONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"season_map[{month!r}] = {season!r} is invalid;"
                    f" allowed values: {sorted(_VALID_SEASONS)}"
                ),
            )


def _validate_day_window(day_window: dict[str, Any]) -> None:
    for key in ("start_hour", "end_hour"):
        val = day_window.get(key)
        if not isinstance(val, int) or not (0 <= val <= 23):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"day_window.{key} must be an integer in 0–23, got {val!r}"
                ),
            )
    if day_window["start_hour"] == day_window["end_hour"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="day_window.start_hour must differ from day_window.end_hour",
        )


def validate_operational_config(config: dict[str, Any]) -> None:
    """Validate a full operational config document; raise 422 on any violation.

    Validates in order: crop → setpoints → season_map → day_window.
    """
    crop = config.get("crop")
    if crop not in CROP_SETPOINT_PROFILES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unknown crop {crop!r};"
                f" known crops: {sorted(CROP_SETPOINT_PROFILES.keys())}"
            ),
        )

    setpoints = config.get("setpoints", {})
    if not isinstance(setpoints, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="setpoints must be an object",
        )
    _validate_setpoints(setpoints)

    season_map = config.get("season_map", {})
    if not isinstance(season_map, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="season_map must be an object",
        )
    _validate_season_map(season_map)

    day_window = config.get("day_window", {})
    if not isinstance(day_window, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="day_window must be an object",
        )
    _validate_day_window(day_window)


async def invalidate_seasonal_caches(tenant_id: str) -> None:
    """Flush cached seasonal σ scalers after an operational-config mutation."""
    from src.models.seasonal_scaler import clear_seasonal_scaler_cache

    clear_seasonal_scaler_cache(tenant_id)
    logger.info("Invalidated seasonal scaler cache for tenant=%s", tenant_id)


async def upsert_operational_config(
    tenant_id: str,
    greenhouse_id: str,
    config: dict[str, Any],
    store: OperationalConfigStore,
    session: AsyncSession,
) -> dict[str, Any]:
    """Validate, persist, and fan-out a new operational config.

    Raises:
        HTTPException 422 if validation fails (nothing written to DB).

    Returns:
        The merged config as ``store.get()`` would return after the write.
    """
    validate_operational_config(config)
    await store.save_to_db(tenant_id, greenhouse_id, config, session)
    logger.info(
        "Upserted operational config: tenant=%s gh=%s crop=%s",
        tenant_id,
        greenhouse_id,
        config.get("crop"),
    )
    await invalidate_response_cache("operational_config")
    await invalidate_seasonal_caches(tenant_id)
    return store.get(tenant_id, greenhouse_id)

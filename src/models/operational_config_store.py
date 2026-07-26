"""Per-tenant/greenhouse operational config store (crop setpoints, season map,
day window, seasonal overrides).

Load order: code defaults built from :mod:`src.config` crop profiles + season map
merged with stored overrides in the ``operational_config`` table.

Deep-merge contract
-------------------
``get(tenant, gh)`` returns the *merged* config (code defaults built for the
stored crop, then DB overrides applied on top at the top-level key granularity).
Fields absent from the DB override are filled from the per-crop code defaults.

``night=null`` in a setpoint band means no separate night setpoint — callers
should fall back to the day band.

Cache
-----
The in-memory cache is process-local and keyed by ``(tenant_id, greenhouse_id)``.
Multiple workers each maintain independent caches; cache coherence is not
guaranteed across processes.  ``reset()`` is provided for test isolation.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import (
    CROP_SETPOINT_PROFILES,
    DEFAULT_CO2_ENRICHMENT,
    DEFAULT_DAY_WINDOW,
    DEFAULT_SEASON_MAP,
)

logger = logging.getLogger(__name__)

# Crop used when a tenant has no stored config (or an unrecognised crop stored).
_DEFAULT_CROP = "tomato"


def default_operational_config(crop: str | None = None) -> dict[str, Any]:
    """Build the baseline operational config for *crop* (falls back to tomato).

    Returned document matches the canonical config_json shape:
    ``crop``, ``setpoints``, ``day_window``, ``season_map``, ``seasonal_overrides``.
    """
    resolved = crop if crop in CROP_SETPOINT_PROFILES else _DEFAULT_CROP
    return {
        "crop": resolved,
        "setpoints": copy.deepcopy(CROP_SETPOINT_PROFILES[resolved]),
        "day_window": dict(DEFAULT_DAY_WINDOW),
        "season_map": dict(DEFAULT_SEASON_MAP),
        "seasonal_overrides": {
            "summer": {"detector_scale": None},
            "winter": {"detector_scale": None},
        },
        # False = conservative default. Without CO2 enrichment equipment the greenhouse
        # runs at ambient (~420 ppm), permanently below typical lower bounds. Setting
        # this to false suppresses CO2_BAND_LOW to prevent alarm fatigue; see
        # src/config.py DEFAULT_CO2_ENRICHMENT for the full rationale.
        "co2_enrichment": DEFAULT_CO2_ENRICHMENT,
    }


class OperationalConfigStore:
    """In-memory operational config store with async DB persistence.

    The cache maps ``(tenant_id, greenhouse_id) -> raw_override_dict`` where
    the raw override dict is exactly what was stored to / loaded from DB.
    ``get()`` deep-merges the per-crop code defaults with that dict.
    """

    def __init__(self) -> None:
        self._overrides: dict[tuple[str, str], dict[str, Any]] = {}

    def get(self, tenant_id: str, greenhouse_id: str) -> dict[str, Any]:
        """Return the deep-merged operational config for (tenant, greenhouse).

        Builds crop-profile code defaults first, then applies stored overrides
        on top key-by-key (whole top-level values replace defaults).
        """
        overrides = self._overrides.get((tenant_id, greenhouse_id), {})
        crop = overrides.get("crop", _DEFAULT_CROP)
        merged = default_operational_config(crop)
        for key, value in overrides.items():
            merged[key] = copy.deepcopy(value)
        return merged

    def get_raw_overrides(
        self, tenant_id: str, greenhouse_id: str
    ) -> dict[str, Any]:
        """Return the raw stored override dict (keys present in DB override).

        Used by the GET endpoint to annotate each top-level key with its source
        ('default' vs 'override').
        """
        return dict(self._overrides.get((tenant_id, greenhouse_id), {}))

    def set_overrides(
        self,
        tenant_id: str,
        greenhouse_id: str,
        overrides: dict[str, Any],
    ) -> None:
        """Replace the in-memory overrides for (tenant, greenhouse)."""
        self._overrides[(tenant_id, greenhouse_id)] = copy.deepcopy(overrides)
        logger.info(
            "Updated in-memory operational config: tenant=%s gh=%s",
            tenant_id,
            greenhouse_id,
        )

    def reset(
        self,
        tenant_id: str | None = None,
        greenhouse_id: str | None = None,
    ) -> None:
        """Clear cached overrides — test isolation helper.

        If both ids are None, clears all cached entries.
        If both ids are provided, clears only that specific (tenant, gh) entry.
        """
        if tenant_id is None and greenhouse_id is None:
            self._overrides.clear()
            return
        key = (tenant_id or "", greenhouse_id or "")
        self._overrides.pop(key, None)

    async def load_from_db(
        self,
        tenant_id: str,
        greenhouse_id: str,
        session: AsyncSession,
    ) -> dict[str, Any]:
        """Load overrides from DB into the in-memory cache.

        Returns the merged config after loading.
        """
        from src.db.models import OperationalConfig

        row = await session.scalar(
            select(OperationalConfig).where(
                OperationalConfig.tenant_id == tenant_id,
                OperationalConfig.greenhouse_id == greenhouse_id,
            )
        )
        if row is not None and row.config_json:
            self._overrides[(tenant_id, greenhouse_id)] = dict(row.config_json)
        return self.get(tenant_id, greenhouse_id)

    async def save_to_db(
        self,
        tenant_id: str,
        greenhouse_id: str,
        config: dict[str, Any],
        session: AsyncSession,
    ) -> None:
        """Persist *config* to the DB (upsert) and update the in-memory cache.

        On first write inserts a new row; on subsequent writes updates in place.
        The cache is updated before ``session.commit()`` so callers see the new
        values immediately even if they call ``get()`` synchronously after.
        """
        from src.db.models import OperationalConfig

        row = await session.scalar(
            select(OperationalConfig).where(
                OperationalConfig.tenant_id == tenant_id,
                OperationalConfig.greenhouse_id == greenhouse_id,
            )
        )
        now = datetime.now(timezone.utc)
        if row is None:
            session.add(
                OperationalConfig(
                    tenant_id=tenant_id,
                    greenhouse_id=greenhouse_id,
                    config_json=config,
                    updated_at=now,
                )
            )
        else:
            row.config_json = config
            row.updated_at = now
        # Update cache before commit so synchronous post-save callers are consistent.
        self._overrides[(tenant_id, greenhouse_id)] = copy.deepcopy(config)
        await session.commit()


_global_store = OperationalConfigStore()


def get_operational_config_store() -> OperationalConfigStore:
    """Return the module-level singleton OperationalConfigStore."""
    return _global_store

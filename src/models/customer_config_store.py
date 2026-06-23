"""
Per-customer model configuration overrides (ensemble weights, thresholds).

Load order: defaults from :mod:`src.config` merged with stored overrides.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import (
    ANOMALY_THRESHOLD,
    ENSEMBLE_ANOMALY_THRESHOLD,
    ENSEMBLE_DETECTOR_WEIGHTS,
    ENSEMBLE_MIN_AGREEMENT,
)

logger = logging.getLogger(__name__)

CONFIG_KEYS = (
    "ANOMALY_THRESHOLD",
    "ENSEMBLE_DETECTOR_WEIGHTS",
    "ENSEMBLE_ANOMALY_THRESHOLD",
    "ENSEMBLE_MIN_AGREEMENT",
)


def default_customer_config() -> Dict[str, Any]:
    """Baseline config merged from src.config defaults."""
    return {
        "ANOMALY_THRESHOLD": ANOMALY_THRESHOLD,
        "ENSEMBLE_DETECTOR_WEIGHTS": dict(ENSEMBLE_DETECTOR_WEIGHTS),
        "ENSEMBLE_ANOMALY_THRESHOLD": ENSEMBLE_ANOMALY_THRESHOLD,
        "ENSEMBLE_MIN_AGREEMENT": ENSEMBLE_MIN_AGREEMENT,
    }


class CustomerConfigStore:
    """In-memory config store with optional async DB persistence."""

    def __init__(self) -> None:
        self._overrides: Dict[str, Dict[str, Any]] = {}
        self._snapshots: Dict[str, Dict[str, Any]] = {}

    def get(self, customer_id: str) -> Dict[str, Any]:
        """Return merged config for customer."""
        merged = default_customer_config()
        overrides = self._overrides.get(customer_id, {})
        for key in CONFIG_KEYS:
            if key in overrides:
                value = overrides[key]
                if key == "ENSEMBLE_DETECTOR_WEIGHTS" and isinstance(value, dict):
                    merged[key] = dict(value)
                else:
                    merged[key] = value
        return merged

    def promote(
        self,
        customer_id: str,
        proposed: Dict[str, Any],
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist proposed config; keep snapshot for rollback."""
        if snapshot is not None:
            self._snapshots[customer_id] = copy.deepcopy(snapshot)
        self._overrides[customer_id] = copy.deepcopy(proposed)
        logger.info("Promoted config for customer %s", customer_id)

    def rollback(self, customer_id: str) -> bool:
        """Restore last snapshot; return True if rollback applied."""
        snapshot = self._snapshots.get(customer_id)
        if snapshot is None:
            return False
        self._overrides[customer_id] = copy.deepcopy(snapshot)
        logger.info("Rolled back config for customer %s", customer_id)
        return True

    def clear(self, customer_id: Optional[str] = None) -> None:
        """Reset overrides (testing helper)."""
        if customer_id is None:
            self._overrides.clear()
            self._snapshots.clear()
            return
        self._overrides.pop(customer_id, None)
        self._snapshots.pop(customer_id, None)

    async def load_from_db(
        self, customer_id: str, session: AsyncSession
    ) -> Dict[str, Any]:
        """Load overrides from DB into memory cache."""
        from src.db.models import CustomerModelConfig

        row = await session.scalar(
            select(CustomerModelConfig).where(
                CustomerModelConfig.customer_id == customer_id
            )
        )
        if row is not None and row.config_json:
            self._overrides[customer_id] = dict(row.config_json)
        return self.get(customer_id)

    async def save_to_db(
        self, customer_id: str, session: AsyncSession
    ) -> None:
        """Persist current overrides to DB."""
        from src.db.models import CustomerModelConfig

        config = self._overrides.get(customer_id, {})
        row = await session.scalar(
            select(CustomerModelConfig).where(
                CustomerModelConfig.customer_id == customer_id
            )
        )
        now = datetime.now(timezone.utc)
        if row is None:
            session.add(
                CustomerModelConfig(
                    customer_id=customer_id,
                    config_json=config,
                    updated_at=now,
                )
            )
        else:
            row.config_json = config
            row.updated_at = now
        await session.commit()


_global_store = CustomerConfigStore()


def get_config_store() -> CustomerConfigStore:
    """Module-level config store singleton."""
    return _global_store

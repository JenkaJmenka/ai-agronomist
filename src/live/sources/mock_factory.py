"""Factory and sanitization helpers for mock SCADA sources.

``build_mock_sources_for_scope`` assembles climate, CO₂, fertigation, and gas
mock sources; ``_clamp_and_sanitize`` is the shared output guard used by
``MockScadaSource._generate_channels``.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict, List

from src.config import MOCK_SCADA_DEMO_MODE, MOCK_SCADA_INTERVAL_SECONDS
from src.live.events import CHANNEL_BOUNDS
from src.live.sources.base import SourceConfig

if TYPE_CHECKING:
    from src.live.sources.mock import MockScadaSource


def _clamp_and_sanitize(channels: Dict[str, float]) -> Dict[str, float]:
    """Clamp to ``CHANNEL_BOUNDS`` and drop non-finite values."""
    out: Dict[str, float] = {}
    for key, val in channels.items():
        if not math.isfinite(val):
            continue
        lo, hi = CHANNEL_BOUNDS.get(key, (val, val))
        out[key] = max(lo, min(hi, val))
    return out


def build_mock_sources_for_scope(
    tenant_id: str,
    greenhouse_id: str,
) -> List["MockScadaSource"]:
    """Build complementary mock sources for a tenant/greenhouse scope.

    Returns
    -------
    list[MockScadaSource]
        - ``mock_climate``: temperature, humidity, light (priority 10).
          Has demo-mode enabled when ``MOCK_SCADA_DEMO_MODE`` is True.
        - ``mock_co2``: co2 only (priority 20, 1.5x interval offset).
        - ``mock_fertigation``: soil_moisture, root_zone_temp (priority 30,
          2x interval offset).
        - ``mock_gas``: c2h4, gas_co2, no, no2, nox, co (priority 40,
          2.5x interval offset) — feeds the Gas Monitoring page.
    """
    # Imported here, not at module top: mock.py imports _clamp_and_sanitize
    # from this module, so a top-level import of MockScadaSource would be
    # circular.
    from src.live.sources.mock import MockScadaSource

    climate = SourceConfig(
        source_id=f"{tenant_id}_{greenhouse_id}_mock_climate",
        vendor="mock",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        channels=["temperature", "humidity", "light"],
        priority=10,
        poll_interval_seconds=MOCK_SCADA_INTERVAL_SECONDS,
        params={"seed": 42, "demo_mode": MOCK_SCADA_DEMO_MODE},
    )
    co2 = SourceConfig(
        source_id=f"{tenant_id}_{greenhouse_id}_mock_co2",
        vendor="mock",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        channels=["co2"],
        priority=20,
        poll_interval_seconds=MOCK_SCADA_INTERVAL_SECONDS * 1.5,
        params={"seed": 137},
    )
    fertigation = SourceConfig(
        source_id=f"{tenant_id}_{greenhouse_id}_mock_fertigation",
        vendor="mock",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        channels=["soil_moisture", "root_zone_temp"],
        priority=30,
        poll_interval_seconds=MOCK_SCADA_INTERVAL_SECONDS * 2,
        params={"seed": 256},
    )
    gas = SourceConfig(
        source_id=f"{tenant_id}_{greenhouse_id}_mock_gas",
        vendor="mock",
        tenant_id=tenant_id,
        greenhouse_id=greenhouse_id,
        channels=["c2h4", "gas_co2", "no", "no2", "nox", "co"],
        priority=40,
        poll_interval_seconds=MOCK_SCADA_INTERVAL_SECONDS * 2.5,
        params={"seed": 404},
    )
    return [
        MockScadaSource(climate),
        MockScadaSource(co2),
        MockScadaSource(fertigation),
        MockScadaSource(gas),
    ]

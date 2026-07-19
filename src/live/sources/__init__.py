"""Live monitoring data-source connectors.

``CONNECTOR_TYPES`` maps vendor strings to concrete connector classes.
"""

from __future__ import annotations

from typing import Dict, Type

from src.live.sources.base import LiveSourceConnector, SourceConfig
from src.live.sources.hoogendoorn import HoogendoornConnector
from src.live.sources.mock import MockScadaSource
from src.live.sources.priva import PrivaConnector

CONNECTOR_TYPES: Dict[str, Type[LiveSourceConnector]] = {
    "mock": MockScadaSource,
    "priva": PrivaConnector,
    "hoogendoorn": HoogendoornConnector,
}

__all__ = [
    "CONNECTOR_TYPES",
    "HoogendoornConnector",
    "LiveSourceConnector",
    "MockScadaSource",
    "PrivaConnector",
    "SourceConfig",
]

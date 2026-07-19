"""Source connector interface for live monitoring data sources.

Defines the configuration model and abstract base class for connecting to
vendor-specific SCADA/sensor systems and streaming readings into the merge
pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, ClassVar, Dict, List

from pydantic import BaseModel

from src.live.events import SourceReading


class SourceConfig(BaseModel):
    """Configuration for a live monitoring data source.

    Vendor-specific params (API URLs, poll topics, etc.) go in ``params``.
    Secrets must be referenced as environment variable NAMES, never literal
    values.

    Attributes
    ----------
    source_id:
        Unique identifier for this source instance.
    vendor:
        Vendor string matching a ``LiveSourceConnector.vendor`` class attr.
    tenant_id:
        Tenant scope.
    greenhouse_id:
        Greenhouse scope.
    channels:
        Canonical channel names this source provides.
    priority:
        Lower wins per-channel when multiple sources supply the same channel.
    poll_interval_seconds:
        Expected cadence between readings (used for health monitoring).
    params:
        Vendor-specific key-value configuration (secrets as env-var names).
    """

    source_id: str
    vendor: str
    tenant_id: str
    greenhouse_id: str
    channels: List[str]
    priority: int = 100
    poll_interval_seconds: float = 5.0
    params: Dict[str, Any] = {}


class LiveSourceConnector(ABC):
    """Abstract base for live SCADA/sensor data-source connectors.

    Concrete implementations set a ``vendor`` class attribute matching the
    string used in :class:`SourceConfig`.  Each connector owns a single
    :class:`SourceConfig` and produces a never-ending async stream of
    :class:`SourceReading` objects.

    Connection errors must raise ``DataSourceConnectionError`` from the
    existing vendor exception hierarchy in ``src/vendors/base.py``.
    """

    vendor: ClassVar[str]

    def __init__(self, config: SourceConfig) -> None:
        self._config = config

    @property
    def config(self) -> SourceConfig:
        """Read-only access to the connector's configuration."""
        return self._config

    @abstractmethod
    async def connect(self) -> None:
        """Establish a connection to the data source.

        Raises
        ------
        DataSourceConnectionError
            If the connection cannot be established.
        """

    @abstractmethod
    def stream(self) -> AsyncIterator[SourceReading]:
        """Yield an infinite async stream of partial sensor readings.

        The generator runs until cancelled or the source is closed.
        Each yielded ``SourceReading`` may cover a subset of channels.
        """

    @abstractmethod
    async def close(self) -> None:
        """Release resources and disconnect from the data source."""

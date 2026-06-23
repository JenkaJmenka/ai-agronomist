"""
Vendors subpackage containing adapters, abstract base classes, and exception hierarchies
for sensor telemetry ingestion.
"""

from .base import (
    AbstractVendorAdapter,
    DataReadError,
    DataSourceAdapter,
    DataSourceConnectionError,
    DataValidationError,
    NormalizationError,
    VendorError,
)
from .csv_adapter import CSVAdapter
from .priva_client import PrivaClient

__all__ = [
    "DataSourceAdapter",
    "AbstractVendorAdapter",
    "CSVAdapter",
    "PrivaClient",
    "VendorError",
    "DataSourceConnectionError",
    "DataReadError",
    "NormalizationError",
    "DataValidationError",
]

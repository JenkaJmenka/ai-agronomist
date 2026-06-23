"""Pydantic v2 response models for the gas sensor anomaly API."""

from typing import Optional

from pydantic import BaseModel


class GasReadingEntry(BaseModel):
    timestamp: str
    value: Optional[float] = None
    is_anomaly: bool
    z_score: float
    severity: str
    method: str


class GasColumnSummary(BaseModel):
    column: str
    mean: float
    std: float
    anomaly_count: int
    current_value: Optional[float] = None
    readings: list[GasReadingEntry]


class GasSummaryResponse(BaseModel):
    gas_columns: list[GasColumnSummary]
    window_hours: int


class GasRawEntry(BaseModel):
    timestamp: str
    data: dict[str, Optional[float]]


class GasReadingsResponse(BaseModel):
    readings: list[GasRawEntry]
    total: int

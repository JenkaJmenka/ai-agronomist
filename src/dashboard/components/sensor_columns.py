"""Climate vs gas column classification for monitoring pages."""

from __future__ import annotations

import pandas as pd

from src.config import GAS_SENSOR_COLUMNS
from src.vendors.csv_adapter import SENSOR_DEFAULTS

_GAS_CANONICAL = {name.lower() for name in GAS_SENSOR_COLUMNS}

_GAS_CSV_ALIASES = {
    "measured_no2",
    "measured_o3",
    "measured_co",
    "measured_voc",
    "measured_particulates",
    "measured_nox",
    "measured_c2h4",
}

_METADATA_COLUMNS = {
    "timestamp",
    "_synthetic",
    "source",
    "anomaly_score",
    "anomaly_columns",
    "anomaly_detectors",
}

# Substrings that identify greenhouse climate / environmental sensors.
_CLIMATE_KEYWORDS = (
    "temp",
    "humidity",
    "humid",
    "light",
    "lux",
    "vent",
    "screen",
    "pipe",
    "wind",
    "plant",
    "outdoor",
    "deficit",
    "heating",
    "illuminance",
    "par",
    "ppfd",
    "w_m",
    "rain",
)

# Canonical columns imputed at upload when absent from the CSV.
_IMPUTED_OPTIONAL_COLUMNS = frozenset(SENSOR_DEFAULTS)


def _normalize(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def is_gas_column(name: str) -> bool:
    norm = _normalize(name)
    if norm in _GAS_CANONICAL:
        return True
    if norm in _GAS_CSV_ALIASES:
        return True
    if norm.startswith("gas_"):
        return True
    if norm in {"co2", "carbon_dioxide"} and not _looks_like_climate_co2(norm):
        return True
    return False


def _looks_like_climate_co2(norm: str) -> bool:
    """CO2 from climate CSVs is environmental; gas_co2 is the dedicated gas sensor."""
    return norm == "co2"


def is_climate_column(name: str) -> bool:
    if name in _METADATA_COLUMNS:
        return False
    if is_gas_column(name):
        return False
    norm = _normalize(name)
    return any(kw in norm for kw in _CLIMATE_KEYWORDS)


def is_imputed_optional_column(df: pd.DataFrame, col: str) -> bool:
    """True when a column is a flat upload default (e.g. co2=400 with no real sensor)."""
    if col not in _IMPUTED_OPTIONAL_COLUMNS or col not in df.columns:
        return False
    series = pd.to_numeric(df[col], errors="coerce").dropna()
    if series.empty:
        return True
    default = SENSOR_DEFAULTS[col]
    return series.nunique() <= 1 and abs(float(series.iloc[0]) - default) < 1e-6


def get_numeric_columns(df: pd.DataFrame) -> list[str]:
    return [
        col
        for col in df.columns
        if col not in _METADATA_COLUMNS and pd.api.types.is_numeric_dtype(df[col])
    ]


def get_all_sensor_columns(df: pd.DataFrame) -> list[str]:
    """All numeric sensor channels (climate + gas + other)."""
    return get_numeric_columns(df)


def get_climate_columns(df: pd.DataFrame) -> list[str]:
    """Climate/environment sensors only; excludes gas and imputed optional defaults."""
    return [
        col
        for col in get_numeric_columns(df)
        if is_climate_column(col) and not is_imputed_optional_column(df, col)
    ]


def get_gas_columns(df: pd.DataFrame) -> list[str]:
    return [col for col in get_numeric_columns(df) if is_gas_column(col)]

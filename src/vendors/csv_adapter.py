import os
import re as _re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.vendors.base import (
    DataSourceAdapter,
    DataSourceConnectionError,
    DataReadError,
    NormalizationError,
    DataValidationError,
)

# ---------------------------------------------------------------------------
# Canonical schema definition — single source of truth for column names
# ---------------------------------------------------------------------------

TIMESTAMP_COL: str = "timestamp"

# Sensors that must be present and non-NaN; rows are dropped when they are NaN.
REQUIRED_SENSOR_COLS: Tuple[str, ...] = ("temperature", "humidity")

# Sensors that may be absent or NaN; filled with SENSOR_DEFAULTS when missing.
# Derived from SENSOR_DEFAULTS so the two are always in sync.
SENSOR_DEFAULTS: Dict[str, float] = {
    "co2": 400.0,   # typical greenhouse CO2 (ppm)
    "light": 0.0,   # unknown → assume dark
}
OPTIONAL_SENSOR_COLS: Tuple[str, ...] = tuple(SENSOR_DEFAULTS)

SENSOR_COLS: Tuple[str, ...] = REQUIRED_SENSOR_COLS + OPTIONAL_SENSOR_COLS
CANONICAL_COLS: Tuple[str, ...] = (TIMESTAMP_COL,) + SENSOR_COLS

# ---------------------------------------------------------------------------
# Column detection: keyword-score matching
# ---------------------------------------------------------------------------
# Each rule: (canonical_col, must_contain_all, must_not_contain_any, score).
# For each canonical target the original column with the highest score wins.
# All checks are case-insensitive substring matches.
_MATCH_RULES: List[Tuple[str, List[str], List[str], int]] = [
    (TIMESTAMP_COL,      ["timestamp"],                        [],                                     100),
    (TIMESTAMP_COL,      ["time"],                             [],                                      50),
    (TIMESTAMP_COL,      ["date"],                             ["update", "created"],                   30),

    ("temperature",      ["greenhouse", "temp"],               [],                                     100),
    ("temperature",      ["compartment", "temp"],              ["pipe", "heating"],                     90),
    ("temperature",      ["air temp"],                         [],                                      80),
    ("temperature",      ["temperature"],                      ["outdoor", "plant", "pipe", "heating"], 60),
    ("temperature",      ["temp"],                             ["outdoor", "plant", "pipe", "heating"], 30),
    # Fallbacks: use plant/outdoor temperature when no indoor sensor is available.
    ("temperature",      ["plant", "temperature"],             ["pipe"],                                20),
    ("temperature",      ["plant", "temp"],                    ["pipe"],                                20),
    ("temperature",      ["outdoor", "temperature"],           [],                                      10),
    ("temperature",      ["outdoor", "temp"],                  [],                                      10),

    ("humidity",         ["relative humidity"],                ["deficit"],                            100),
    ("humidity",         ["humidity", "%"],                    ["deficit"],                             80),
    ("humidity",         ["humidity"],                         ["deficit"],                             50),
    ("humidity",         ["rh"],                               [],                                      40),

    ("co2",              ["co2"],                              [],                                     100),
    ("co2",              ["carbon dioxide"],                   [],                                     100),
    ("co2",              ["co₂"],                              [],                                     100),

    ("light",            ["light intensity"],                  [],                                     100),
    ("light",            ["light", "w/m"],                     [],                                      90),
    ("light",            ["lux"],                              [],                                      80),
    ("light",            ["light"],                            ["day", "sun", "screen"],                50),
    ("light",            ["illuminance", "par", "ppfd"],       [],                                      50),
]


def _detect_csv_properties(filepath: str) -> Dict[str, Any]:
    """Auto-detect delimiter, encoding, and decimal separator.

    Reads the first 4 KB to determine:
    - encoding: UTF-8 with or without BOM
    - sep: ',' vs ';' (whichever appears more often in the header line)
    - decimal: '.' vs ',' (inferred from numeric fields in the second line)
    """
    with open(filepath, "rb") as fh:
        raw = fh.read(4096)

    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    text = raw.decode(encoding, errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header = lines[0] if lines else ""

    sep = ";" if header.count(";") > header.count(",") else ","

    decimal = "."
    if len(lines) > 1 and sep == ";":
        for field in lines[1].split(sep)[1:]:
            stripped = field.strip().replace("-", "")
            if "," in stripped:
                candidate = stripped.replace(",", ".")
                try:
                    float(candidate)
                    decimal = ","
                    break
                except ValueError:
                    pass

    return {"sep": sep, "encoding": encoding, "decimal": decimal}


def _smart_column_map(columns: List[str]) -> Dict[str, str]:
    """Return {original_col: canonical_col} using keyword-score matching.

    Each canonical target is assigned at most once (highest score wins).
    Columns already named with a canonical name are given the maximum score
    so they are never displaced by a higher-scoring alias.
    """
    canonical_set = set(CANONICAL_COLS)
    col_lower = {col: col.lower() for col in columns}

    # canonical → (best_score, best_original_col)
    best: Dict[str, Tuple[int, str]] = {}

    for col, col_l in col_lower.items():
        if col in canonical_set:
            best[col] = (999, col)
            continue
        for canonical, must_have, must_not, score in _MATCH_RULES:
            if all(kw in col_l for kw in must_have) and not any(kw in col_l for kw in must_not):
                current = best.get(canonical)
                if current is None or score > current[0]:
                    best[canonical] = (score, col)

    return {orig_col: canonical for canonical, (_, orig_col) in best.items()}


def _remap_columns(data: pd.DataFrame, explicit: Dict[str, str]) -> pd.DataFrame:
    """Apply explicit mapping then auto-detect any remaining unmapped columns."""
    if explicit:
        data = data.rename(columns=explicit)
    auto = {
        src: dst
        for src, dst in _smart_column_map(list(data.columns)).items()
        if src not in set(CANONICAL_COLS)
    }
    if auto:
        data = data.rename(columns=auto)
    return data


def _fill_missing_columns(data: pd.DataFrame) -> pd.DataFrame:
    """Add optional sensor columns that are entirely absent from the file."""
    for col, default in SENSOR_DEFAULTS.items():
        if col not in data.columns:
            data = data.copy()
            data[col] = default
    return data


def _coerce_sensors(data: pd.DataFrame, normalized: pd.DataFrame) -> pd.DataFrame:
    """Coerce all sensor columns to float64 and store in normalized."""
    for col in SENSOR_COLS:
        try:
            normalized[col] = data[col].astype("float64")
        except Exception as exc:
            raise NormalizationError(f"Failed to coerce column '{col}' to float64") from exc
    return normalized


def _handle_nan(normalized: pd.DataFrame) -> pd.DataFrame:
    """Fill NaN in optional columns with defaults; drop rows with NaN in required columns.

    Order matters: fill optionals first so a row that is only missing optional
    values survives the required-column drop that follows.
    """
    for col in OPTIONAL_SENSOR_COLS:
        if col in normalized.columns:
            normalized[col] = normalized[col].fillna(SENSOR_DEFAULTS[col])
    return normalized.dropna(subset=list((TIMESTAMP_COL,) + REQUIRED_SENSOR_COLS))


# ---------------------------------------------------------------------------
# Raw multi-column parsing — preserves ALL numeric sensor columns
# ---------------------------------------------------------------------------

# Column substrings that indicate identifier/metadata, not sensor readings.
# These are skipped when building the data dict.
# "id" intentionally excluded — it matches "humidity" (hum-id-ity).
# Use boundary-anchored variants instead: " id" (space+id) and "_id".
_NON_DATA_PATTERNS: Tuple[str, ...] = ("serial", "number", "source", " id", "_id", "rain")


def _clean_col_name(col: str) -> str:
    """Strip source prefix and unit/system suffix, return snake_case key."""
    col = _re.sub(r"^(greenhouse_|uman_)", "", col, flags=_re.IGNORECASE)
    col = _re.sub(r"\s*\[.*?\]", "", col)          # remove [connext2:...] suffix
    col = _re.sub(r"\s*\(.*?\)", " ", col)         # remove (°C) etc — keep meaning, drop symbol
    col = _re.sub(r"[^\w\s]+", " ", col)           # drop non-word chars
    col = _re.sub(r"\s+", "_", col.strip()).lower()
    return col.strip("_")


def _to_float_robust(val: Any) -> Optional[float]:
    """Convert a value to float, handling comma decimals (European format)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s in ("nan", "NaN", "NA", "N/A", ""):
        return None
    try:
        return float(s)
    except ValueError:
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return None


def parse_raw_rows(
    chunk: pd.DataFrame,
) -> Tuple[pd.DataFrame, List[str]]:
    """
    Parse a raw CSV chunk, preserving all numeric columns.

    Returns (result_df, col_names) where result_df has:
      - timestamp: UTC datetime
      - source: str (which timestamp column provided the timestamp)
      - one column per detected numeric sensor

    Drops rows with no parseable timestamp.
    """
    # --- Detect timestamp columns ---
    ts_candidates = [
        c for c in chunk.columns
        if any(kw in c.lower() for kw in ("timestamp", "datetime", "date", "time"))
        and "update" not in c.lower()
        and "created" not in c.lower()
    ]

    # Build a unified timestamp series — try each candidate, fill gaps
    ts_series: pd.Series = pd.Series(
        pd.NaT, index=chunk.index, dtype="datetime64[ns, UTC]"
    )
    source_series: pd.Series = pd.Series("", index=chunk.index, dtype=str)

    for col in ts_candidates:
        mask = ts_series.isna() & chunk[col].notna() & (chunk[col].astype(str).str.strip() != "")
        if not mask.any():
            continue
        try:
            parsed = pd.to_datetime(
                chunk.loc[mask, col].astype(str).str.strip(),
                format="mixed",
                dayfirst=True,  # handles DD.MM.YYYY; safe for MM/DD/YYYY when day <= 12
                errors="coerce",
                utc=True,
            )
            ts_series[mask] = parsed
            source_series[mask] = col
        except Exception:
            pass

    # Attach unified timestamp; drop rows with no parseable timestamp
    work = chunk.copy()
    work["_timestamp"] = ts_series
    work["_source"] = source_series
    work = work.dropna(subset=["_timestamp"]).reset_index(drop=True)

    if work.empty:
        return pd.DataFrame(), []

    # --- Collect numeric sensor columns ---
    skip_patterns = _NON_DATA_PATTERNS + tuple(c.lower() for c in ts_candidates)

    result_data: Dict[str, list] = {
        "timestamp": list(work["_timestamp"]),
        "source": list(work["_source"]),
    }
    col_names: List[str] = []

    for col in chunk.columns:
        col_l = col.lower()
        if any(pat in col_l for pat in skip_patterns):
            continue
        clean = _clean_col_name(col)
        if not clean or len(clean) < 2:
            continue
        floats = [_to_float_robust(v) for v in work[col]]
        if all(v is None for v in floats):
            continue  # entirely empty column for this chunk
        result_data[clean] = floats
        if clean not in col_names:
            col_names.append(clean)

    return pd.DataFrame(result_data), col_names


class CSVAdapter(DataSourceAdapter):
    """Concrete DataSourceAdapter for CSV sensor data.

    Accepts files with any delimiter, encoding, decimal style, and column
    naming convention.  Column mapping is auto-detected via keyword scoring;
    optional sensor columns absent from the file are filled with defaults.
    """

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.file_path = self.config.get("filepath")
        self._connected = False
        self._csv_props: Dict[str, Any] = {"sep": ",", "encoding": "utf-8", "decimal": "."}

    def connect(self) -> None:
        if not self.file_path:
            raise DataSourceConnectionError("Filepath not specified in config.")
        if not os.path.exists(self.file_path):
            raise DataSourceConnectionError(f"CSV file not found at path: {self.file_path}")
        self._csv_props = _detect_csv_properties(self.file_path)
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def read(self) -> pd.DataFrame:
        if not getattr(self, "_connected", False):
            raise DataReadError("Adapter is not connected.")
        if not self.file_path:
            raise DataReadError("Filepath not specified in config.")
        try:
            return pd.read_csv(
                self.file_path,
                sep=self._csv_props["sep"],
                encoding=self._csv_props["encoding"],
                decimal=self._csv_props["decimal"],
            )
        except Exception as exc:
            raise DataReadError(f"Failed to read raw CSV data at {self.file_path}: {exc}") from exc

    def normalize(self, data: pd.DataFrame) -> pd.DataFrame:
        """Normalize any CSV to the canonical schema defined by CANONICAL_COLS.

        1. Remap columns: explicit config mapping, then keyword-score auto-detection.
        2. Add any absent optional sensor columns filled with their defaults.
        3. Validate that all required columns are now present.
        4. Parse timestamp → UTC datetime64; coerce sensors → float64.
        5. Fill NaN in optional columns with defaults; drop rows with NaN in required columns.
        """
        if data is None:
            raise NormalizationError("Cannot normalize a None DataFrame.")

        data = _remap_columns(data, self.config.get("column_mapping") or {})
        data = _fill_missing_columns(data)

        required = (TIMESTAMP_COL,) + REQUIRED_SENSOR_COLS
        missing = [c for c in required if c not in data.columns]
        if missing:
            raise NormalizationError(
                f"CSV is missing required columns: {missing}. "
                f"Available columns after mapping: {list(data.columns)}"
            )

        normalized = pd.DataFrame()
        try:
            ts = pd.to_datetime(data[TIMESTAMP_COL], errors="raise", format="mixed", dayfirst=True)
            normalized[TIMESTAMP_COL] = ts.dt.tz_localize("UTC") if ts.dt.tz is None else ts.dt.tz_convert("UTC")
        except Exception as exc:
            raise NormalizationError(f"Failed to normalize '{TIMESTAMP_COL}' column: {exc}") from exc

        normalized = _coerce_sensors(data, normalized)
        return _handle_nan(normalized)

    def validate(self, data: pd.DataFrame) -> bool:
        try:
            return self.validate_canonical_schema(data)
        except DataValidationError as exc:
            raise exc
        except Exception as exc:
            raise DataValidationError(f"Unexpected data validation failure: {exc}") from exc

    @property
    def name(self) -> str:
        return "csv"

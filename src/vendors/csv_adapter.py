import logging
import os
import re as _re
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.config import (
    CSV_UPLOAD_RAW_SOURCE,
    LIGHT_CANONICAL_UNIT,
    LIGHT_LUX_TO_W_PER_M2 as LUX_TO_W_PER_M2,
    LIGHT_PPFD_TO_W_PER_M2 as PPFD_UMOL_TO_W_PER_M2,
)
from src.vendors.base import (
    DataSourceAdapter,
    DataSourceConnectionError,
    DataReadError,
    NormalizationError,
    DataValidationError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical schema definition — single source of truth for column names
# ---------------------------------------------------------------------------

TIMESTAMP_COL: str = "timestamp"

# Sensors that must be present and non-NaN; rows are dropped when they are NaN.
REQUIRED_SENSOR_COLS: Tuple[str, ...] = ("temperature", "humidity")

# Sensors that may be absent or NaN; filled with SENSOR_DEFAULTS when missing.
# Derived from SENSOR_DEFAULTS so the two are always in sync.
SENSOR_DEFAULTS: Dict[str, float] = {
    "co2": 400.0,   # midpoint of typical CO2 range when sensor absent
    "light": 500.0,  # midpoint of light range when sensor absent (not 0)
}
OPTIONAL_SENSOR_COLS: Tuple[str, ...] = tuple(SENSOR_DEFAULTS)

SENSOR_COLS: Tuple[str, ...] = REQUIRED_SENSOR_COLS + OPTIONAL_SENSOR_COLS
CANONICAL_COLS: Tuple[str, ...] = (TIMESTAMP_COL,) + SENSOR_COLS

# ---------------------------------------------------------------------------
# Light-channel unit tracking and conversion
# ---------------------------------------------------------------------------
# LIGHT_CANONICAL_UNIT, LUX_TO_W_PER_M2, and PPFD_UMOL_TO_W_PER_M2 are
# imported from src.config — that is the single source of truth.
# LUX_TO_W_PER_M2 / PPFD_UMOL_TO_W_PER_M2 are local aliases for the
# config.py names LIGHT_LUX_TO_W_PER_M2 / LIGHT_PPFD_TO_W_PER_M2.
# See config.py for source references and rationale.


class LightInputUnit(Enum):
    """Physical unit detected from an original CSV light-column header."""

    W_PER_M2 = "W/m²"                          # solar irradiance — native Connext unit
    LUX = "lux"                                  # illuminance
    PPFD_UMOL = "µmol·m⁻²·s⁻¹"                 # photosynthetic photon flux density
    DEFAULTED_W_PER_M2 = "W/m² (defaulted)"    # no unit marker in header; default applied


def infer_light_unit(column_header: str) -> LightInputUnit:
    """Infer the physical unit of a light column from its original CSV header.

    Checks the header text for known unit markers before any cleaning/stripping.
    Returns ``LightInputUnit.DEFAULTED_W_PER_M2`` when the header carries no
    recognisable unit — the default is documented and logged, never silent.

    Parameters
    ----------
    column_header:
        Original (unmodified) CSV column header string.
    """
    h = column_header.lower()
    # Irradiance markers — match before checking generic "light" to avoid
    # accidentally classifying "w/m" fragments in other contexts.
    if any(kw in h for kw in ("w/m²", "w/m2", "w/m", "w·m", "wm⁻²", "irradiance")):
        return LightInputUnit.W_PER_M2
    # Illuminance markers
    if any(kw in h for kw in ("lux", "illuminance", " lx")):
        return LightInputUnit.LUX
    # Photosynthetic photon flux density markers
    if any(kw in h for kw in ("ppfd", "par", "µmol", "umol", "μmol")):
        return LightInputUnit.PPFD_UMOL
    # No unit marker found — default to W/m² and record the defaulting.
    return LightInputUnit.DEFAULTED_W_PER_M2


def _convert_light_to_canonical(
    series: pd.Series,
    source_unit: LightInputUnit,
) -> pd.Series:
    """Convert a light series from its detected unit to the canonical W/m² unit.

    W/m² and DEFAULTED_W/m² inputs are returned unchanged (no copy made).
    """
    if source_unit in (LightInputUnit.W_PER_M2, LightInputUnit.DEFAULTED_W_PER_M2):
        return series
    if source_unit == LightInputUnit.LUX:
        return series * LUX_TO_W_PER_M2
    if source_unit == LightInputUnit.PPFD_UMOL:
        return series * PPFD_UMOL_TO_W_PER_M2
    return series  # unknown variant — pass through unchanged


def _detect_light_unit_from_df(data: pd.DataFrame) -> LightInputUnit:
    """Find which column will map to 'light' and return its inferred source unit.

    Examines original column headers (before renaming) so that unit markers
    stripped by _clean_col_name (e.g. "(W/m²)") are still visible.
    Returns DEFAULTED_W_PER_M2 when no light column is found.
    """
    col_map = _smart_column_map(list(data.columns))
    for orig_col, canonical in col_map.items():
        if canonical == "light":
            unit = infer_light_unit(orig_col)
            if unit == LightInputUnit.DEFAULTED_W_PER_M2:
                logger.warning(
                    "light column %r carries no unit marker — defaulting to W/m²",
                    orig_col,
                )
            return unit
    # No light column matched — will be filled with the fill default (500.0 W/m²).
    return LightInputUnit.DEFAULTED_W_PER_M2


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

    # Measured humidity deficit (opt-in climate-extras channel, not a model input).
    # Lines 57-59 above intentionally EXCLUDE "deficit" from the humidity channel;
    # these rules capture columns that contain the measured HD value (g/m³).
    ("measured_hd",      ["humidity", "deficit"],              [],                                      90),
    ("measured_hd",      ["hd", "g"],                         [],                                      70),
    ("measured_hd",      ["hd"],                              [],                                      30),
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


def pandas_csv_reader_kwargs(csv_props: Dict[str, Any]) -> Dict[str, Any]:
    """Shared pandas.read_csv kwargs for all upload / inference CSV readers."""
    return {
        "sep": csv_props.get("sep", ","),
        "encoding": csv_props.get("encoding", "utf-8"),
        "decimal": csv_props.get("decimal", "."),
        "on_bad_lines": "skip",
        "skipinitialspace": True,
        "quotechar": '"',
    }


def detect_csv_properties_from_bytes(file_bytes: bytes) -> Dict[str, Any]:
    """Like _detect_csv_properties but for in-memory uploads (validator path)."""
    raw = file_bytes[:4096]
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


def detect_optional_sensor_presence(columns: List[str]) -> tuple[frozenset[str], frozenset[str]]:
    """Return which optional sensors are present in CSV headers vs imputed at normalize."""
    mapped_targets = set(_smart_column_map(columns).values())
    mapped_targets.update(col for col in columns if col in OPTIONAL_SENSOR_COLS)
    present = frozenset(col for col in OPTIONAL_SENSOR_COLS if col in mapped_targets)
    imputed = frozenset(col for col in OPTIONAL_SENSOR_COLS if col not in present)
    return present, imputed


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


# Canonical gas / climate-extra remaps applied on the raw upload path so
# Monitoring / Gas pages (exact channel filters) see predictable names.
# IMPORTANT: plain ``co2`` must NEVER become ``gas_co2`` (climate CO₂).
_RAW_COLUMN_ALIASES: Dict[str, str] = {
    "measured_c2h4": "c2h4",
    "c_2_h_4": "c2h4",
    "ethylene": "c2h4",
    "measured_ethylene": "c2h4",
    "measured_no": "no",
    "measured_no2": "no2",
    "measured_nox": "nox",
    "measured_co": "co",
    "gas_co2": "gas_co2",
    "gas_co_2": "gas_co2",
    "measured_gas_co2": "gas_co2",
    "humidity_deficit": "measured_hd",
    "measured_humidity_deficit": "measured_hd",
    "hd": "measured_hd",
    # Map Connext climate headers onto canonical Monitoring channel names.
    "measured_greenhouse_temp": "temperature",
    "greenhouse_temp": "temperature",
    "measured_relative_humidity": "humidity",
    "relative_humidity": "humidity",
    "light_intensity": "light",
}


def _canonicalize_raw_column(clean: str) -> str:
    """Map cleaned CSV headers onto canonical channel names for raw storage."""
    if clean in _RAW_COLUMN_ALIASES:
        return _RAW_COLUMN_ALIASES[clean]
    if clean.startswith("measured_") and clean[len("measured_"):] in {
        "c2h4", "no", "no2", "nox", "co",
    }:
        return clean[len("measured_"):]
    return clean


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
      - source: str, always ``CSV_UPLOAD_RAW_SOURCE`` -- a fixed provenance
        marker, NOT the name of the CSV column that supplied the timestamp.
        (Older versions of this function wrote that column name here; it
        collided with the "live" / "synthetic:<...>" markers used elsewhere
        on the same RawReading.source column, so the per-row tracking was
        dropped rather than relocated -- see CSV_UPLOAD_RAW_SOURCE's
        docstring in src/config.py.)
      - one column per detected numeric sensor

    Drops rows with no parseable timestamp. Gas / climate-extra aliases are
    remapped onto canonical names (``measured_c2h4`` → ``c2h4``, etc.) so the
    Gas Monitoring exact filter and live mock share the same keys.

    Light-unit handling: the original column header is inspected (before
    ``_clean_col_name`` strips unit tokens) to detect the physical unit.  The
    ``light`` column in the result is always in the canonical W/m² unit.  The
    detected source unit is stored in ``result_df.attrs["light_source_unit"]``.
    """
    # --- Detect timestamp columns ---
    ts_candidates = [
        c for c in chunk.columns
        if any(kw in c.lower() for kw in ("timestamp", "datetime", "date", "time"))
        and "update" not in c.lower()
        and "created" not in c.lower()
    ]

    # Build a unified timestamp series — try each candidate, fill gaps.
    ts_series: pd.Series = pd.Series(
        pd.NaT, index=chunk.index, dtype="datetime64[ns, UTC]"
    )

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
        except Exception:
            pass

    # Attach unified timestamp; drop rows with no parseable timestamp
    work = chunk.copy()
    work["_timestamp"] = ts_series
    work = work.dropna(subset=["_timestamp"]).reset_index(drop=True)

    if work.empty:
        return pd.DataFrame(), []

    # --- Collect numeric sensor columns ---
    skip_patterns = _NON_DATA_PATTERNS + tuple(c.lower() for c in ts_candidates)

    result_data: Dict[str, list] = {
        "timestamp": list(work["_timestamp"]),
        # Fixed provenance marker, not the timestamp column's header -- see
        # CSV_UPLOAD_RAW_SOURCE's docstring in src/config.py.
        "source": [CSV_UPLOAD_RAW_SOURCE] * len(work),
    }
    col_names: List[str] = []
    # Track which light source unit was detected (recorded in df.attrs).
    light_source_unit: Optional[LightInputUnit] = None

    for col in chunk.columns:
        col_l = col.lower()
        if any(pat in col_l for pat in skip_patterns):
            continue
        clean = _canonicalize_raw_column(_clean_col_name(col))
        if not clean or len(clean) < 2:
            continue
        floats = [_to_float_robust(v) for v in work[col]]
        if all(v is None for v in floats):
            continue  # entirely empty column for this chunk
        # Prefer the first occurrence when aliases collide (e.g. no vs measured_no).
        if clean in result_data:
            continue

        # Light-unit conversion — detect from the ORIGINAL header (before cleaning
        # strips unit tokens like "(W/m²)") and convert to canonical W/m².
        if clean == "light" and light_source_unit is None:
            light_source_unit = infer_light_unit(col)
            if light_source_unit == LightInputUnit.DEFAULTED_W_PER_M2:
                logger.warning(
                    "parse_raw_rows: light column %r carries no unit marker — defaulting to W/m²",
                    col,
                )
            floats = [
                v * LUX_TO_W_PER_M2 if (v is not None and light_source_unit == LightInputUnit.LUX)
                else v * PPFD_UMOL_TO_W_PER_M2 if (v is not None and light_source_unit == LightInputUnit.PPFD_UMOL)
                else v
                for v in floats
            ]

        result_data[clean] = floats
        if clean not in col_names:
            col_names.append(clean)

    result_df = pd.DataFrame(result_data)
    result_df.attrs["light_source_unit"] = (
        light_source_unit if light_source_unit is not None else LightInputUnit.DEFAULTED_W_PER_M2
    )
    return result_df, col_names


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
                **pandas_csv_reader_kwargs(self._csv_props),
            )
        except Exception as exc:
            raise DataReadError(f"Failed to read raw CSV data at {self.file_path}: {exc}") from exc

    def normalize(self, data: pd.DataFrame) -> pd.DataFrame:
        """Normalize any CSV to the canonical schema defined by CANONICAL_COLS.

        1. Detect the physical unit of the light column from original headers.
        2. Remap columns: explicit config mapping, then keyword-score auto-detection.
        3. Add any absent optional sensor columns filled with their defaults.
        4. Validate that all required columns are now present.
        5. Parse timestamp → UTC datetime64; coerce sensors → float64.
        6. Convert the light column to canonical W/m² (see LIGHT_CANONICAL_UNIT).
        7. Fill NaN in optional columns with defaults; drop rows with NaN in required columns.

        The detected source unit is stored in ``result.attrs["light_source_unit"]``
        so callers can log or display the provenance of the light values.
        """
        if data is None:
            raise NormalizationError("Cannot normalize a None DataFrame.")

        # Step 1 — detect light unit from original headers BEFORE renaming strips units.
        light_unit = _detect_light_unit_from_df(data)

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

        # Step 6 — convert light to canonical W/m².
        if "light" in normalized.columns:
            normalized["light"] = _convert_light_to_canonical(normalized["light"], light_unit)

        result = _handle_nan(normalized)
        result.attrs["light_source_unit"] = light_unit
        return result

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

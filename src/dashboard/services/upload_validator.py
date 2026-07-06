"""Client-side CSV validation before upload to the inference API."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pandas as pd

from src.vendors.csv_adapter import detect_csv_properties_from_bytes, pandas_csv_reader_kwargs

_MAX_FILE_SIZE_MB = 50
_MAX_FILE_SIZE_BYTES = _MAX_FILE_SIZE_MB * 1024 * 1024
_STRICT_SAMPLE_ROWS = 2000


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _friendly_csv_parse_error(message: str) -> str:
    line_hint = ""
    match = re.search(r"line (\d+)", message, flags=re.IGNORECASE)
    if match:
        line_hint = f" (around line {match.group(1)})"
    return (
        f"CSV format error{line_hint}: {message}. "
        "This usually means unquoted commas in a text field, mixed comma/semicolon "
        "delimiters, or extra columns on some rows. Re-export as UTF-8 CSV with comma "
        "separators and quote text fields, or fix the row and try again."
    )


def validate_csv_upload(file_bytes: bytes, filename: str = "") -> ValidationResult:
    """Validate CSV bytes before sending to the inference API.

  Checks size, delimiter detection, and row structure. Rows with inconsistent
  column counts are skipped server-side (same as training upload); when detected
  in the sample we still allow upload but warn the operator.
    """
    if len(file_bytes) == 0:
        return ValidationResult(is_valid=False, errors=["The file is empty."])

    if len(file_bytes) > _MAX_FILE_SIZE_BYTES:
        size_mb = len(file_bytes) / (1024 * 1024)
        return ValidationResult(
            is_valid=False,
            errors=[
                f"File is too large ({size_mb:.1f} MB). "
                f"Maximum allowed size is {_MAX_FILE_SIZE_MB} MB."
            ],
        )

    csv_props = detect_csv_properties_from_bytes(file_bytes)
    strict_kwargs = {**pandas_csv_reader_kwargs(csv_props), "on_bad_lines": "error"}

    warnings: list[str] = []
    try:
        df = pd.read_csv(
            io.BytesIO(file_bytes),
            nrows=_STRICT_SAMPLE_ROWS,
            **strict_kwargs,
        )
    except Exception as exc:
        permissive_kwargs = pandas_csv_reader_kwargs(csv_props)
        try:
            df = pd.read_csv(
                io.BytesIO(file_bytes),
                nrows=_STRICT_SAMPLE_ROWS,
                **permissive_kwargs,
            )
        except Exception:
            return ValidationResult(
                is_valid=False,
                errors=[_friendly_csv_parse_error(str(exc))],
            )
        warnings.append(
            "Some rows have inconsistent column counts and will be skipped during "
            f"processing. {_friendly_csv_parse_error(str(exc))}"
        )

    if df.empty:
        return ValidationResult(
            is_valid=False,
            errors=["The CSV has no data rows (only a header or is completely empty)."],
        )

    return ValidationResult(is_valid=True, warnings=warnings)

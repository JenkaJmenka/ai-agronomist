"""Client-side CSV validation before upload to the inference API."""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pandas as pd

_REQUIRED_COLUMNS = frozenset({"temperature", "humidity", "co2", "light"})
_MAX_FILE_SIZE_MB = 50
_MAX_FILE_SIZE_BYTES = _MAX_FILE_SIZE_MB * 1024 * 1024


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_csv_upload(file_bytes: bytes, filename: str = "") -> ValidationResult:
    """Validate CSV bytes before sending to the inference API.

    Checks: non-empty, size limit, parseable, required columns present.
    Returns a ValidationResult with human-readable errors/warnings.
    """
    errors: list[str] = []
    warnings: list[str] = []

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

    try:
        df = pd.read_csv(io.BytesIO(file_bytes), nrows=5, on_bad_lines="skip")
    except Exception as exc:
        return ValidationResult(
            is_valid=False,
            errors=[
                "Could not parse the file as CSV. "
                "Check that it uses comma delimiters and has a header row."
            ],
        )

    if df.empty:
        return ValidationResult(
            is_valid=False,
            errors=["The CSV has no data rows (only a header or is completely empty)."],
        )

    actual_cols = {str(c).strip().lower() for c in df.columns}
    missing = _REQUIRED_COLUMNS - actual_cols
    if missing:
        errors.append(
            f"Missing required columns: {', '.join(sorted(missing))}. "
            f"Required: {', '.join(sorted(_REQUIRED_COLUMNS))}."
        )

    if errors:
        return ValidationResult(is_valid=False, errors=errors, warnings=warnings)

    return ValidationResult(is_valid=True, errors=errors, warnings=warnings)

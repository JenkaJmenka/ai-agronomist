"""Client-side CSV validation before upload to the inference API."""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pandas as pd

_MAX_FILE_SIZE_MB = 50
_MAX_FILE_SIZE_BYTES = _MAX_FILE_SIZE_MB * 1024 * 1024


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _detect_sep(file_bytes: bytes) -> tuple[str, str]:
    """Detect delimiter and encoding from the first 4 KB."""
    raw = file_bytes[:4096]
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    text = raw.decode(encoding, errors="replace")
    header = next((ln for ln in text.splitlines() if ln.strip()), "")
    sep = ";" if header.count(";") > header.count(",") else ","
    return sep, encoding


def validate_csv_upload(file_bytes: bytes, filename: str = "") -> ValidationResult:
    """Validate CSV bytes before sending to the inference API.

    Only checks what the client can definitively judge: empty file, size limit,
    and basic parseability. Column validation is delegated to the API which uses
    fuzzy keyword matching and handles semicolon/comma delimiters automatically.
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

    sep, encoding = _detect_sep(file_bytes)
    try:
        df = pd.read_csv(
            io.BytesIO(file_bytes),
            nrows=5,
            sep=sep,
            encoding=encoding,
            on_bad_lines="skip",
        )
    except Exception:
        return ValidationResult(
            is_valid=False,
            errors=[
                "Could not parse the file as CSV. "
                "Check that it uses comma or semicolon delimiters and has a header row."
            ],
        )

    if df.empty:
        return ValidationResult(
            is_valid=False,
            errors=["The CSV has no data rows (only a header or is completely empty)."],
        )

    return ValidationResult(is_valid=True)

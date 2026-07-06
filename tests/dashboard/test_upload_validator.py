"""Tests for src/dashboard/services/upload_validator.py."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from src.dashboard.services.upload_validator import ValidationResult, validate_csv_upload

_REQUIRED = {"temperature", "humidity", "co2", "light"}

_VALID_ROW = {
    "timestamp": ["2024-01-01 00:00:00"],
    "temperature": [22.5],
    "humidity": [65.0],
    "co2": [410.0],
    "light": [500.0],
}


def _csv(data: dict) -> bytes:
    return pd.DataFrame(data).to_csv(index=False).encode()


# ---------------------------------------------------------------------------
# (a) Malformed CSV scenarios
# ---------------------------------------------------------------------------


def test_empty_bytes_returns_invalid():
    r = validate_csv_upload(b"")
    assert not r.is_valid
    assert any("empty" in e.lower() for e in r.errors)


def test_whitespace_only_returns_invalid():
    r = validate_csv_upload(b"   \n\n  ")
    assert not r.is_valid


def test_header_only_no_data_rows_returns_invalid():
    csv_bytes = b"temperature,humidity,co2,light\n"
    r = validate_csv_upload(csv_bytes)
    assert not r.is_valid
    assert any("no data" in e.lower() or "empty" in e.lower() or "header" in e.lower() for e in r.errors)


def test_all_required_columns_missing_passes():
    csv_bytes = _csv({"timestamp": ["2024-01-01"], "sensor_x": [1.0]})
    r = validate_csv_upload(csv_bytes)
    assert r.is_valid


def test_single_missing_column_passes():
    data = dict(_VALID_ROW)
    del data["light"]
    csv_bytes = _csv(data)
    r = validate_csv_upload(csv_bytes)
    assert r.is_valid


def test_binary_non_csv_content_returns_invalid():
    r = validate_csv_upload(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    assert not r.is_valid
    err_text = " ".join(r.errors).lower()
    assert "csv" in err_text or "parse" in err_text


def test_semicolon_delimiter_passes():
    csv_bytes = b"temperature;humidity;co2;light\n22.5;65.0;410.0;500.0\n"
    r = validate_csv_upload(csv_bytes)
    assert r.is_valid


# ---------------------------------------------------------------------------
# (b) Valid CSV passes
# ---------------------------------------------------------------------------


def test_valid_csv_with_all_required_columns_passes():
    r = validate_csv_upload(_csv(_VALID_ROW))
    assert r.is_valid, r.errors


def test_valid_csv_with_extra_columns_passes():
    data = dict(_VALID_ROW)
    data["extra_sensor"] = [99.0]
    r = validate_csv_upload(_csv(data))
    assert r.is_valid, r.errors


def test_column_names_case_insensitive():
    data = {
        "Timestamp": ["2024-01-01 00:00:00"],
        "Temperature": [22.5],
        "HUMIDITY": [65.0],
        "Co2": [410.0],
        "Light": [500.0],
    }
    r = validate_csv_upload(_csv(data))
    assert r.is_valid, r.errors


def test_valid_csv_no_extra_columns_passes():
    data = {
        "temperature": [22.5],
        "humidity": [65.0],
        "co2": [410.0],
        "light": [500.0],
    }
    r = validate_csv_upload(_csv(data))
    assert r.is_valid, r.errors


# ---------------------------------------------------------------------------
# (c) File size limit
# ---------------------------------------------------------------------------


def test_oversized_file_returns_invalid():
    # 51 MB of zeros — well over the 50 MB limit
    big = b"x" * (51 * 1024 * 1024)
    r = validate_csv_upload(big)
    assert not r.is_valid
    err_text = " ".join(r.errors).lower()
    assert "large" in err_text or "size" in err_text or "mb" in err_text


def test_file_just_under_limit_with_valid_content_passes():
    # Build a CSV that is valid but small enough to be under 50 MB
    r = validate_csv_upload(_csv(_VALID_ROW))
    assert r.is_valid


# ---------------------------------------------------------------------------
# (d) Error message quality
# ---------------------------------------------------------------------------


def test_empty_file_error_is_human_readable():
    r = validate_csv_upload(b"")
    assert r.errors
    assert len(r.errors[0]) > 10  # not just a code or empty string
    assert r.errors[0][0].isupper()  # starts with capital letter


def test_missing_column_passes():
    csv_bytes = _csv({"timestamp": ["2024-01-01"], "pressure": [1013.0]})
    r = validate_csv_upload(csv_bytes)
    assert r.is_valid


def test_parse_error_mentions_csv_format():
    r = validate_csv_upload(b"\x00\x01\x02\x03")
    assert not r.is_valid
    err_text = " ".join(r.errors).lower()
    assert "csv" in err_text


def test_valid_result_has_no_errors():
    r = validate_csv_upload(_csv(_VALID_ROW))
    assert r.is_valid
    assert r.errors == []


def test_malformed_row_warns_but_allows_upload():
    # Header has 3 cols; row 3 has extra unquoted fields (simulates line 4 error).
    csv_bytes = b"timestamp,temperature,humidity\n2024-01-01,22,65\n2024-01-02,23,66\nbad,row,with,extra,cols\n"
    r = validate_csv_upload(csv_bytes)
    assert r.is_valid, r.errors
    assert r.warnings
    assert "skipped" in r.warnings[0].lower()


def test_validation_result_is_dataclass():
    r = validate_csv_upload(_csv(_VALID_ROW))
    assert isinstance(r, ValidationResult)
    assert hasattr(r, "is_valid")
    assert hasattr(r, "errors")
    assert hasattr(r, "warnings")

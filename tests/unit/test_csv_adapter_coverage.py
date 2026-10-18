"""Additional tests targeting uncovered branches in src/vendors/csv_adapter.py.

Targets:
  Lines 94-103   — _detect_csv_properties: semicolon sep + comma decimal path
  Line  206      — _to_float_robust: early return for empty/sentinel strings
  Lines 209-213  — _to_float_robust: comma-decimal fallback and failure path
  Line  246      — parse_raw_rows: try block entered when mask.any() is True
  Line  267      — parse_raw_rows: empty-result early return
  Line  284      — parse_raw_rows: non-data pattern column skipped
  Lines 373-374  — CSVAdapter.validate: unexpected exception wrapped as DataValidationError
"""
from __future__ import annotations

import pytest
import pandas as pd
from unittest.mock import patch

from src.config import CSV_UPLOAD_RAW_SOURCE
from src.vendors.csv_adapter import (
    CSVAdapter,
    _detect_csv_properties,
    _smart_column_map,
    _to_float_robust,
    parse_raw_rows,
)
from src.vendors.base import DataValidationError


# ---------------------------------------------------------------------------
# _detect_csv_properties  (lines 94-103)
# ---------------------------------------------------------------------------

class TestDetectCsvPropertiesSemicolonComma:
    """Semicolon delimiter + comma decimal detection."""

    def test_semicolon_with_comma_decimal(self, tmp_path: pytest.TempPathFactory) -> None:
        """Lines 94-103: second line has comma-decimal numeric fields."""
        content = (
            "timestamp;temperature;humidity\n"
            "2026-06-09 12:00:00;22,5;60,2\n"
            "2026-06-09 13:00:00;23,1;58,9\n"
        )
        filepath = tmp_path / "semi_comma.csv"
        filepath.write_text(content, encoding="utf-8")
        props = _detect_csv_properties(str(filepath))
        assert props["sep"] == ";"
        assert props["decimal"] == ","

    def test_semicolon_with_dot_decimal_stays_dot(self, tmp_path: pytest.TempPathFactory) -> None:
        """Semicolon separator with standard dot decimal."""
        content = (
            "timestamp;temperature;humidity\n"
            "2026-06-09 12:00:00;22.5;60.2\n"
        )
        filepath = tmp_path / "semi_dot.csv"
        filepath.write_text(content, encoding="utf-8")
        props = _detect_csv_properties(str(filepath))
        assert props["sep"] == ";"
        assert props["decimal"] == "."

    def test_semicolon_non_parseable_comma_stays_dot(self, tmp_path: pytest.TempPathFactory) -> None:
        """Comma in field that is not a valid float → decimal stays '.'."""
        content = (
            "timestamp;label;value\n"
            "2026-06-09 12:00:00;foo,bar;ok\n"
        )
        filepath = tmp_path / "non_parseable.csv"
        filepath.write_text(content, encoding="utf-8")
        props = _detect_csv_properties(str(filepath))
        assert props["sep"] == ";"
        assert props["decimal"] == "."

    def test_comma_sep_detected(self, tmp_path: pytest.TempPathFactory) -> None:
        content = "timestamp,temperature,humidity\n2026-06-09 12:00:00,22.5,60.2\n"
        filepath = tmp_path / "comma.csv"
        filepath.write_text(content, encoding="utf-8")
        props = _detect_csv_properties(str(filepath))
        assert props["sep"] == ","

    def test_bom_encoding_detected(self, tmp_path: pytest.TempPathFactory) -> None:
        content = "timestamp,temperature\n2026-06-09 12:00:00,22.5\n"
        filepath = tmp_path / "bom.csv"
        filepath.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
        props = _detect_csv_properties(str(filepath))
        assert props["encoding"] == "utf-8-sig"

    def test_negative_comma_decimal_detected(self, tmp_path: pytest.TempPathFactory) -> None:
        """Negative value with comma decimal (e.g. '-3,5') — strips dash then checks."""
        content = (
            "timestamp;temperature\n"
            "2026-06-09 12:00:00;-3,5\n"
        )
        filepath = tmp_path / "neg_comma.csv"
        filepath.write_text(content, encoding="utf-8")
        props = _detect_csv_properties(str(filepath))
        assert props["sep"] == ";"
        assert props["decimal"] == ","


# ---------------------------------------------------------------------------
# _to_float_robust  (lines 206, 209-213)
# ---------------------------------------------------------------------------

class TestToFloatRobust:
    """Edge-case inputs for the robust float converter."""

    # Line 206 — empty / sentinel returns None
    def test_empty_string_returns_none(self) -> None:
        assert _to_float_robust("") is None

    def test_nan_strings_return_none(self) -> None:
        for sentinel in ("nan", "NaN", "NA", "N/A"):
            assert _to_float_robust(sentinel) is None, f"Expected None for {sentinel!r}"

    def test_whitespace_only_returns_none(self) -> None:
        assert _to_float_robust("   ") is None

    # Lines 209-211 — comma decimal fallback (line 208 raises, line 211 succeeds)
    def test_comma_decimal_parses_to_float(self) -> None:
        result = _to_float_robust("22,5")
        assert result == pytest.approx(22.5)

    def test_comma_decimal_negative(self) -> None:
        result = _to_float_robust("-3,14")
        assert result == pytest.approx(-3.14)

    # Lines 212-213 — both direct parse and comma-decimal parse fail → None
    def test_non_parseable_string_returns_none(self) -> None:
        assert _to_float_robust("foo,bar") is None

    def test_text_with_comma_returns_none(self) -> None:
        assert _to_float_robust("abc,def") is None

    # Existing normal paths (should still pass)
    def test_valid_float_string(self) -> None:
        assert _to_float_robust("3.14") == pytest.approx(3.14)

    def test_none_input_returns_none(self) -> None:
        assert _to_float_robust(None) is None

    def test_float_nan_returns_none(self) -> None:
        assert _to_float_robust(float("nan")) is None


# ---------------------------------------------------------------------------
# parse_raw_rows  (lines 246, 267, 284, 289->278)
# ---------------------------------------------------------------------------

class TestParseRawRows:
    """Tests targeting uncovered branches inside parse_raw_rows()."""

    def test_no_timestamp_column_returns_empty_df(self) -> None:
        """Line 267: all rows have NaT timestamp → work.empty → early return."""
        chunk = pd.DataFrame({"temperature": [22.5, 23.0], "humidity": [60.0, 61.0]})
        result_df, col_names = parse_raw_rows(chunk)
        assert result_df.empty
        assert col_names == []

    def test_all_null_timestamp_column_returns_empty(self) -> None:
        """All timestamp candidates are None → no parseable row → empty result."""
        chunk = pd.DataFrame({"timestamp": [None, None], "temperature": [22.5, 23.0]})
        result_df, col_names = parse_raw_rows(chunk)
        assert result_df.empty

    def test_valid_timestamp_enters_try_block(self) -> None:
        """Line 246: try block entered when mask.any() is True (non-null ts values)."""
        chunk = pd.DataFrame({
            "timestamp": ["2026-06-09 12:00:00", "2026-06-09 13:00:00"],
            "temperature": [22.5, 23.0],
        })
        result_df, col_names = parse_raw_rows(chunk)
        assert not result_df.empty
        # Confirms the try block executed and produced results
        assert "temperature" in col_names

    def test_non_data_pattern_column_skipped(self) -> None:
        """Line 284: columns matching _NON_DATA_PATTERNS (serial, _id, etc.) are skipped."""
        chunk = pd.DataFrame({
            "timestamp": ["2026-06-09 12:00:00", "2026-06-09 13:00:00"],
            "temperature": [22.5, 23.0],
            "device_serial": [1001, 1002],   # "serial" pattern → skipped
            "sensor_id": [7, 8],             # "_id" pattern → skipped
        })
        result_df, col_names = parse_raw_rows(chunk)
        assert "device_serial" not in col_names
        assert "sensor_id" not in col_names
        assert "temperature" in col_names

    def test_all_none_column_excluded_from_results(self) -> None:
        """Lines 285-286 (289->278 branch): all-None column values → column skipped."""
        chunk = pd.DataFrame({
            "timestamp": ["2026-06-09 12:00:00", "2026-06-09 13:00:00"],
            "temperature": [22.5, 23.0],
            "ghost_sensor": [None, None],
        })
        result_df, col_names = parse_raw_rows(chunk)
        assert "ghost_sensor" not in col_names
        assert "temperature" in col_names

    def test_short_cleaned_column_name_skipped(self) -> None:
        """Columns whose cleaned name is shorter than 2 chars are skipped."""
        chunk = pd.DataFrame({
            "timestamp": ["2026-06-09 12:00:00"],
            "x": [99.0],          # cleaned to "x" (len == 1) → skipped
            "temperature": [22.5],
        })
        result_df, col_names = parse_raw_rows(chunk)
        assert "x" not in col_names
        assert "temperature" in col_names

    def test_multiple_valid_columns_produce_results(self) -> None:
        """Happy path: multiple sensor columns all returned (covers 289->278 loop)."""
        chunk = pd.DataFrame({
            "timestamp": ["2026-06-09 12:00:00", "2026-06-09 13:00:00"],
            "temperature": [22.5, 23.0],
            "humidity": [60.0, 61.0],
            "co2": [410.0, 415.0],
        })
        result_df, col_names = parse_raw_rows(chunk)
        assert not result_df.empty
        assert len(col_names) >= 3

    def test_source_column_carries_the_fixed_csv_marker_not_the_header(self) -> None:
        """``source`` is the fixed CSV provenance marker, never the ts header.

        This assertion used to read ``== "datetime"``: the column carried the
        name of whichever CSV column supplied the timestamp.  That made a
        user-controlled header string land in ``RawReading.source``, the same
        column ``wipe_live_demo_data`` reads as provenance, so a header like
        ``live:timestamp`` got a whole upload deleted by a live-data wipe.
        ``parse_raw_rows`` now writes ``CSV_UPLOAD_RAW_SOURCE`` regardless of
        the file's headers — see its docstring in ``src/config.py``.
        """
        chunk = pd.DataFrame({
            "datetime": ["2026-06-09T12:00:00"],
            "temperature": [22.5],
        })
        result_df, _ = parse_raw_rows(chunk)
        assert not result_df.empty
        assert "source" in result_df.columns
        assert result_df["source"].iloc[0] == CSV_UPLOAD_RAW_SOURCE

    def test_source_marker_is_identical_for_every_timestamp_header(self) -> None:
        """Two files whose ts columns are headed differently agree on ``source``.

        The behaviour that makes collision with a reserved marker structurally
        impossible rather than merely unlikely: the value no longer varies
        with the input at all.
        """
        datetime_headed = pd.DataFrame({
            "datetime": ["2026-06-09T12:00:00"],
            "temperature": [22.5],
        })
        reserved_marker_headed = pd.DataFrame({
            "live:timestamp": ["2026-06-09T12:00:00"],
            "temperature": [22.5],
        })
        from_datetime, _ = parse_raw_rows(datetime_headed)
        from_reserved, _ = parse_raw_rows(reserved_marker_headed)

        assert list(from_datetime["source"]) == list(from_reserved["source"])
        assert set(from_reserved["source"]) == {CSV_UPLOAD_RAW_SOURCE}


# ---------------------------------------------------------------------------
# _smart_column_map  (branch 128->125)
# ---------------------------------------------------------------------------

class TestSmartColumnMap:
    """Covers the loop-continuation branch (128->125) in _smart_column_map."""

    def test_non_canonical_column_matched_and_loop_continues(self) -> None:
        """When a non-canonical col matches a rule, the inner loop continues
        to subsequent rules — this covers the 128->125 branch."""
        # "greenhouse temperature reading" matches rule for "temperature"
        result = _smart_column_map(["greenhouse temperature reading", "relative humidity %"])
        assert "greenhouse temperature reading" in result or len(result) > 0

    def test_auto_detect_ambient_temp(self) -> None:
        """Column 'ambient_temp' is auto-detected as 'temperature'."""
        result = _smart_column_map(["ambient_temp"])
        # Should map to temperature via the "temp" rule
        assert "ambient_temp" in result

    def test_canonical_column_mapped_to_itself(self) -> None:
        """Canonical column names get score 999 and map to themselves."""
        result = _smart_column_map(["temperature", "humidity"])
        assert result.get("temperature") == "temperature"


# ---------------------------------------------------------------------------
# CSVAdapter.validate  (lines 373-374)
# ---------------------------------------------------------------------------

class TestCSVAdapterValidateExceptionPaths:
    """Covers the exception-handling paths in CSVAdapter.validate()."""

    def test_unexpected_exception_wraps_as_data_validation_error(self) -> None:
        """Lines 373-374: non-DataValidationError is caught and re-raised
        as DataValidationError."""
        adapter = CSVAdapter({"filepath": "dummy.csv"})
        with patch.object(
            adapter, "validate_canonical_schema", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(DataValidationError, match="Unexpected data validation failure"):
                adapter.validate(pd.DataFrame())

    def test_data_validation_error_propagates_unchanged(self) -> None:
        """DataValidationError is re-raised directly without wrapping."""
        adapter = CSVAdapter({"filepath": "dummy.csv"})
        with patch.object(
            adapter,
            "validate_canonical_schema",
            side_effect=DataValidationError("schema violation"),
        ):
            with pytest.raises(DataValidationError, match="schema violation"):
                adapter.validate(pd.DataFrame())

    def test_type_error_wrapped_as_validation_error(self) -> None:
        """TypeError (another non-DataValidationError) is also wrapped."""
        adapter = CSVAdapter({"filepath": "dummy.csv"})
        with patch.object(
            adapter,
            "validate_canonical_schema",
            side_effect=TypeError("unexpected type"),
        ):
            with pytest.raises(DataValidationError, match="Unexpected data validation failure"):
                adapter.validate(pd.DataFrame())

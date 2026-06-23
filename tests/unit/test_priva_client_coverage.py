"""Additional tests targeting uncovered branches in src/vendors/priva_client.py.

Targets:
  Lines 85, 87    — read(): string start_time / end_time → pd.to_datetime()
  Lines 91, 93    — read(): tz-naive start_dt / end_dt → .replace(tzinfo=utc)
  Lines 113-114   — read(): exception → DataReadError
  Line  138       — normalize(): missing required column → NormalizationError
  Line  146       — normalize(): tz-naive "time" column → tz_localize("UTC")
  Lines 157-158   — normalize(): exception in transform block → NormalizationError
  Lines 175-176   — validate(): DataValidationError re-raised directly
  Lines 177-178   — validate(): unexpected exception wrapped as DataValidationError
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
from unittest.mock import patch

from src.vendors.base import DataReadError, DataValidationError, NormalizationError
from src.vendors.priva_client import PrivaClient


@pytest.fixture()
def connected() -> PrivaClient:
    """A PrivaClient that is already connected."""
    client = PrivaClient(config={"api_key": "test_key_123"})
    client.connect()
    return client


# ---------------------------------------------------------------------------
# read() — string timestamps (lines 85, 87) and tz-naive (lines 91, 93)
# ---------------------------------------------------------------------------

class TestReadWithStringTimestamps:
    def test_string_start_and_end_time_are_parsed(self, connected: PrivaClient) -> None:
        """Lines 85, 87: string timestamps are converted via pd.to_datetime().
        Lines 91, 93: the resulting naive Timestamp gets UTC tzinfo added."""
        connected.config["start_time"] = "2026-01-01 00:00:00"
        connected.config["end_time"] = "2026-01-02 00:00:00"
        df = connected.read()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 24

    def test_string_timestamps_produce_correct_columns(self, connected: PrivaClient) -> None:
        """Returned DataFrame from string-timestamp read has expected Priva columns."""
        connected.config["start_time"] = "2026-06-01T00:00:00"
        connected.config["end_time"] = "2026-06-02T00:00:00"
        df = connected.read()
        for col in ("time", "ambient_temp", "rel_humidity", "co2_level", "radiation_lux"):
            assert col in df.columns

    def test_tz_naive_datetime_gets_utc_added(self, connected: PrivaClient) -> None:
        """Lines 91, 93: datetime objects without tzinfo trigger the .replace() branch."""
        connected.config["start_time"] = datetime(2026, 3, 1, 8, 0)   # no tzinfo
        connected.config["end_time"] = datetime(2026, 3, 2, 8, 0)     # no tzinfo
        df = connected.read()
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 24


# ---------------------------------------------------------------------------
# read() — exception path (lines 113-114)
# ---------------------------------------------------------------------------

class TestReadExceptionPath:
    def test_exception_inside_read_raises_data_read_error(
        self, connected: PrivaClient
    ) -> None:
        """Lines 113-114: any exception in the try block is re-raised as DataReadError."""
        with patch("pandas.date_range", side_effect=ValueError("simulated failure")):
            with pytest.raises(DataReadError, match="Failed to read telemetry stream from Priva"):
                connected.read()

    def test_data_read_error_message_contains_original_reason(
        self, connected: PrivaClient
    ) -> None:
        with patch("pandas.date_range", side_effect=RuntimeError("boom")):
            with pytest.raises(DataReadError, match="boom"):
                connected.read()


# ---------------------------------------------------------------------------
# normalize() — missing required Priva column (line 138)
# ---------------------------------------------------------------------------

class TestNormalizeMissingColumn:
    _REQUIRED_COLS = ("time", "ambient_temp", "rel_humidity", "co2_level", "radiation_lux")

    def _full_row(self) -> dict:
        return {
            "time": "2026-06-09T12:00:00+00:00",
            "ambient_temp": 22.5,
            "rel_humidity": 60.0,
            "co2_level": 410.0,
            "radiation_lux": 750.0,
        }

    def test_missing_rel_humidity_raises(self, connected: PrivaClient) -> None:
        """Line 138: NormalizationError raised for missing 'rel_humidity' column."""
        row = self._full_row()
        del row["rel_humidity"]
        with pytest.raises(NormalizationError, match="rel_humidity"):
            connected.normalize(pd.DataFrame([row]))

    def test_missing_co2_level_raises(self, connected: PrivaClient) -> None:
        row = self._full_row()
        del row["co2_level"]
        with pytest.raises(NormalizationError, match="co2_level"):
            connected.normalize(pd.DataFrame([row]))

    def test_missing_radiation_lux_raises(self, connected: PrivaClient) -> None:
        row = self._full_row()
        del row["radiation_lux"]
        with pytest.raises(NormalizationError, match="radiation_lux"):
            connected.normalize(pd.DataFrame([row]))

    def test_missing_ambient_temp_raises(self, connected: PrivaClient) -> None:
        row = self._full_row()
        del row["ambient_temp"]
        with pytest.raises(NormalizationError, match="ambient_temp"):
            connected.normalize(pd.DataFrame([row]))

    def test_missing_time_raises(self, connected: PrivaClient) -> None:
        row = self._full_row()
        del row["time"]
        with pytest.raises(NormalizationError, match="time"):
            connected.normalize(pd.DataFrame([row]))


# ---------------------------------------------------------------------------
# normalize() — tz-naive timestamps (line 146)
# ---------------------------------------------------------------------------

class TestNormalizeTzNaiveTimestamps:
    def test_tz_naive_time_column_is_localized_to_utc(self, connected: PrivaClient) -> None:
        """Line 146: 'time' column without timezone → tz_localize("UTC")."""
        data = pd.DataFrame({
            "time": ["2026-06-09T12:00:00"],   # valid ISO8601, no timezone
            "ambient_temp": [22.5],
            "rel_humidity": [60.0],
            "co2_level": [410.0],
            "radiation_lux": [750.0],
        })
        result = connected.normalize(data)
        assert result["timestamp"].dt.tz is not None
        assert str(result["timestamp"].dt.tz) in ("UTC", "utc")

    def test_tz_aware_time_column_is_converted_to_utc(self, connected: PrivaClient) -> None:
        """The tz_convert branch (else path, line 148): tz-aware → convert to UTC."""
        data = pd.DataFrame({
            "time": ["2026-06-09T12:00:00+00:00"],  # tz-aware ISO8601
            "ambient_temp": [22.5],
            "rel_humidity": [60.0],
            "co2_level": [410.0],
            "radiation_lux": [750.0],
        })
        result = connected.normalize(data)
        assert result["timestamp"].dt.tz is not None


# ---------------------------------------------------------------------------
# normalize() — exception in transform block (lines 157-158)
# ---------------------------------------------------------------------------

class TestNormalizeExceptionPath:
    def test_invalid_time_format_raises_normalization_error(
        self, connected: PrivaClient
    ) -> None:
        """Lines 157-158: pd.to_datetime raises → caught → NormalizationError."""
        data = pd.DataFrame({
            "time": ["not-a-valid-iso8601-date"],
            "ambient_temp": [22.5],
            "rel_humidity": [60.0],
            "co2_level": [410.0],
            "radiation_lux": [750.0],
        })
        with pytest.raises(NormalizationError, match="Error transforming Priva data"):
            connected.normalize(data)

    def test_normalization_error_wraps_original_exception(
        self, connected: PrivaClient
    ) -> None:
        """The NormalizationError should chain the original exception."""
        data = pd.DataFrame({
            "time": ["INVALID"],
            "ambient_temp": [22.5],
            "rel_humidity": [60.0],
            "co2_level": [410.0],
            "radiation_lux": [750.0],
        })
        try:
            connected.normalize(data)
        except NormalizationError as exc:
            assert exc.__cause__ is not None


# ---------------------------------------------------------------------------
# validate() — exception handler paths (lines 175-178)
# ---------------------------------------------------------------------------

class TestValidateExceptionPaths:
    def test_data_validation_error_is_re_raised_directly(
        self, connected: PrivaClient
    ) -> None:
        """Lines 175-176: DataValidationError propagates unchanged."""
        original = DataValidationError("column 'temperature' out of range")
        with patch.object(
            connected, "validate_canonical_schema", side_effect=original
        ):
            with pytest.raises(DataValidationError, match="column 'temperature' out of range"):
                connected.validate(pd.DataFrame())

    def test_unexpected_exception_wrapped_as_data_validation_error(
        self, connected: PrivaClient
    ) -> None:
        """Lines 177-178: RuntimeError → wrapped as DataValidationError."""
        with patch.object(
            connected,
            "validate_canonical_schema",
            side_effect=RuntimeError("unexpected internal crash"),
        ):
            with pytest.raises(DataValidationError, match="Unexpected validation error occurred"):
                connected.validate(pd.DataFrame())

    def test_wrapped_error_message_contains_original_reason(
        self, connected: PrivaClient
    ) -> None:
        """The wrapper message includes the original exception text."""
        with patch.object(
            connected,
            "validate_canonical_schema",
            side_effect=ValueError("type mismatch"),
        ):
            with pytest.raises(DataValidationError, match="type mismatch"):
                connected.validate(pd.DataFrame())

    def test_attribute_error_wrapped_as_data_validation_error(
        self, connected: PrivaClient
    ) -> None:
        """AttributeError (another non-DataValidationError) is also wrapped."""
        with patch.object(
            connected,
            "validate_canonical_schema",
            side_effect=AttributeError("missing attribute"),
        ):
            with pytest.raises(DataValidationError, match="Unexpected validation error occurred"):
                connected.validate(pd.DataFrame())

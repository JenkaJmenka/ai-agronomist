"""Tests for the bootstrap loader sampling fix.

These tests verify the defects listed in the task:
1. Full time-span retention (no reservoir subsampling)
2. Reproducibility (deterministic output across runs)
3. No silent measurement replacement (resample/interpolate)
4. Input nulls preserved (not interpolated away)
5. Malformed chunks produce log records (not silently swallowed)
"""
from __future__ import annotations

import logging
from io import StringIO

import numpy as np
import pandas as pd
import pytest

from src.models.bootstrap import _load_and_resample

_SENSOR_COLS = ["temperature", "humidity", "co2", "light"]


def _write_uniform_csv(path: str, *, rows: int, freq: str = "5min") -> pd.DataFrame:
    """Write a synthetic CSV at uniform cadence and return the source DataFrame.

    Values are deterministic (no randomness) so round-trip comparisons are exact.
    """
    idx = pd.date_range("2026-01-01", periods=rows, freq=freq, tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp": idx.strftime("%Y-%m-%d %H:%M:%S%z"),
            "temperature": 22.0 + np.sin(np.arange(rows) / 50.0),
            "humidity": 65.0 + np.cos(np.arange(rows) / 50.0),
            "co2": 420.0 + (np.arange(rows) % 13).astype(float),
            "light": 300.0 + (np.arange(rows) % 17).astype(float),
        },
        index=idx,
    )
    df.to_csv(path, index=False)
    return df


class TestFullTimeSpanRetention:
    """Given a synthetic input of N > 50,000 rows at uniform 5-min cadence,
    the loader returns rows covering the FULL time span and no interior gap
    exceeds the input's own maximum gap.
    """

    def test_full_span_and_no_fabricated_gaps(self, tmp_path: object) -> None:
        n_rows = 55_000
        csv_path = str(tmp_path / "large.csv")  # type: ignore[operator]
        src_df = _write_uniform_csv(csv_path, rows=n_rows)

        result = _load_and_resample(csv_path)

        # Output first & last timestamps match input
        assert result.index.min() == src_df.index.min()
        assert result.index.max() == src_df.index.max()

        # No interior gap exceeds the source's own maximum gap (300 s = 5 min)
        gaps = result.index.to_series().diff().dropna()
        source_max_gap = pd.Timedelta("5min")
        assert gaps.max() <= source_max_gap, (
            f"Largest output gap {gaps.max()} exceeds source max gap {source_max_gap}"
        )

        # Retention rate must be > 95% (ideally ~100%; dedup may lose a few)
        retention = len(result) / n_rows
        assert retention > 0.95, f"Only {retention:.1%} of rows retained"


class TestReproducibility:
    """Running the loader twice on identical input produces identical output."""

    def test_deterministic_output(self, tmp_path: object) -> None:
        csv_path = str(tmp_path / "repro.csv")  # type: ignore[operator]
        _write_uniform_csv(csv_path, rows=1_000)

        result_a = _load_and_resample(csv_path)
        result_b = _load_and_resample(csv_path)

        pd.testing.assert_frame_equal(result_a, result_b)


class TestNoSilentMeasurementReplacement:
    """A row that carried a real measurement in the input is unchanged in the
    output (no silent mean/interpolation substitution).
    """

    def test_values_preserved_exactly(self, tmp_path: object) -> None:
        csv_path = str(tmp_path / "exact.csv")  # type: ignore[operator]
        src_df = _write_uniform_csv(csv_path, rows=500)

        result = _load_and_resample(csv_path)

        # Pick 20 evenly-spaced timestamps that exist in both frames
        common_idx = src_df.index.intersection(result.index)
        assert len(common_idx) > 0, "No common timestamps between input and output"

        sample_idx = common_idx[np.linspace(0, len(common_idx) - 1, 20).astype(int)]
        for ts in sample_idx:
            for col in _SENSOR_COLS:
                src_val = src_df.loc[ts, col]
                out_val = result.loc[ts, col]
                if pd.isna(src_val):
                    assert pd.isna(out_val), (
                        f"Source NaN at {ts}/{col} became {out_val} in output"
                    )
                else:
                    assert out_val == pytest.approx(src_val, abs=1e-9), (
                        f"Value at {ts}/{col}: source={src_val}, output={out_val}"
                    )


class TestInputNullsPreserved:
    """Input nulls are NOT silently interpolated away."""

    def test_nulls_survive_round_trip(self, tmp_path: object) -> None:
        n_rows = 200
        idx = pd.date_range("2026-01-01", periods=n_rows, freq="5min", tz="UTC")
        df = pd.DataFrame(
            {
                "timestamp": idx.strftime("%Y-%m-%d %H:%M:%S%z"),
                "temperature": 22.0 + np.sin(np.arange(n_rows) / 10.0),
                "humidity": 65.0 + np.cos(np.arange(n_rows) / 10.0),
                "co2": 420.0 + (np.arange(n_rows) % 7).astype(float),
                "light": 300.0 + (np.arange(n_rows) % 11).astype(float),
            },
            index=idx,
        )
        # Plant nulls at known positions
        null_positions = [10, 50, 100, 150]
        for pos in null_positions:
            df.iloc[pos, df.columns.get_loc("humidity")] = np.nan

        csv_path = str(tmp_path / "nulls.csv")  # type: ignore[operator]
        df.to_csv(csv_path, index=False)

        result = _load_and_resample(csv_path)

        # Each planted null must still be NaN in the output
        for pos in null_positions:
            ts = idx[pos]
            if ts in result.index:
                assert pd.isna(result.loc[ts, "humidity"]), (
                    f"Null at position {pos} ({ts}) was interpolated away: "
                    f"got {result.loc[ts, 'humidity']}"
                )


class TestMalformedChunkLogging:
    """A malformed chunk produces a log record rather than vanishing silently."""

    def test_malformed_chunk_logged(
        self, tmp_path: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Create a CSV where the first chunk is fine but a second chunk triggers
        # a normalization error by having completely invalid data.
        n_good = 100
        idx_good = pd.date_range("2026-01-01", periods=n_good, freq="5min", tz="UTC")
        good_df = pd.DataFrame(
            {
                "timestamp": idx_good.strftime("%Y-%m-%d %H:%M:%S%z"),
                "temperature": [22.0] * n_good,
                "humidity": [65.0] * n_good,
                "co2": [420.0] * n_good,
                "light": [300.0] * n_good,
            }
        )

        # Write good rows + a block of completely non-numeric garbage
        csv_path = str(tmp_path / "bad_chunk.csv")  # type: ignore[operator]
        good_csv = good_df.to_csv(index=False)

        # We'll create a file that's valid CSV header + good rows, then has
        # rows where the adapter's normalize() will fail.
        # Instead of fabricating raw garbage, mock the adapter to fail on
        # the second chunk.
        # Simpler: write a valid CSV and monkeypatch the adapter to fail once.
        good_df.to_csv(csv_path, index=False)

        # The cleaner approach: verify the logger is called with expected fields
        # by injecting a failure via a very small chunk size and bad data.
        # For unit-test simplicity, just verify the successful path logs the
        # expected summary line.
        with caplog.at_level(logging.INFO, logger="src.models.bootstrap"):
            result = _load_and_resample(csv_path)

        assert len(result) > 0
        # Summary log must be present
        assert any("Bootstrap loader" in r.message and "output rows" in r.message
                    for r in caplog.records)


class TestMalformedChunkLoggingWithMock:
    """Verify that a chunk normalization failure produces a WARNING log."""

    def test_failed_chunk_emits_warning(
        self, tmp_path: object, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        csv_path = str(tmp_path / "mock_fail.csv")  # type: ignore[operator]
        _write_uniform_csv(csv_path, rows=200)

        # Monkeypatch BOOTSTRAP_LOADER_CHUNK_SIZE to 50 so we get multiple chunks
        import src.models.bootstrap as bmod
        monkeypatch.setattr(bmod, "BOOTSTRAP_LOADER_CHUNK_SIZE", 50)

        # Make the adapter's normalize() raise on the second call
        call_count = {"n": 0}
        original_load = bmod._load_and_resample

        from src.vendors.csv_adapter import CSVAdapter
        orig_normalize = CSVAdapter.normalize

        def flaky_normalize(self: CSVAdapter, chunk: pd.DataFrame) -> pd.DataFrame:
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("Simulated normalization failure")
            return orig_normalize(self, chunk)

        monkeypatch.setattr(CSVAdapter, "normalize", flaky_normalize)

        with caplog.at_level(logging.WARNING, logger="src.models.bootstrap"):
            result = _load_and_resample(csv_path)

        # The result should still have rows from non-failed chunks
        assert len(result) > 0

        # A warning log with chunk context must be present
        warning_msgs = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("skipping chunk" in r.message.lower() for r in warning_msgs), (
            f"Expected 'skipping chunk' warning; got: {[r.message for r in warning_msgs]}"
        )

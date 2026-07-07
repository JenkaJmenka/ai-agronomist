"""Tests for the two-pass cold-start bootstrap (physics + IF cleaning, VAE scoring)."""

import numpy as np
import pandas as pd
import pytest

from src.config import SEQ_LEN
from src.models import bootstrap as bootstrap_module
from src.models.bootstrap import (
    ColdStartBootstrap,
    InsufficientDataError,
    Pass2BootstrapDetector,
    _cap_training_sequences,
    _classify_root_cause,
    _compute_if_flags,
    _compute_physics_flags,
    _df_to_array,
    _extract_timestamps,
    _load_and_resample,
)

_SENSOR_COLS = ["temperature", "humidity", "co2", "light"]


def _write_csv(path, rows: int, mutate=None) -> str:
    idx = pd.date_range("2026-06-01", periods=rows, freq="5min", tz="UTC")
    df = pd.DataFrame(
        {
            "temperature": 22.0 + np.sin(np.arange(rows) / 10.0),
            "humidity": 60.0 + np.cos(np.arange(rows) / 10.0),
            "co2": 420.0 + (np.arange(rows) % 7),
            "light": 300.0 + (np.arange(rows) % 11),
        },
        index=idx,
    )
    if mutate is not None:
        mutate(df)
    df.to_csv(path)
    return str(path)


class TestCapTrainingSequences:
    def _make(self, count: int) -> np.ndarray:
        return np.arange(count * SEQ_LEN * 4, dtype=np.float32).reshape(
            count, SEQ_LEN, 4
        )

    def test_returns_unchanged_when_under_cap(self, monkeypatch):
        monkeypatch.setattr(bootstrap_module, "BOOTSTRAP_MAX_TRAIN_SEQUENCES", 1000)
        seqs = self._make(200)
        assert _cap_training_sequences(seqs) is seqs

    def test_subsamples_to_cap_with_in_bounds_indices(self, monkeypatch):
        monkeypatch.setattr(bootstrap_module, "BOOTSTRAP_MAX_TRAIN_SEQUENCES", 100)
        seqs = self._make(1000)
        capped = _cap_training_sequences(seqs)
        assert len(capped) == 100
        # Even stride keeps coverage across the whole range, not just the head.
        assert np.array_equal(capped[0], seqs[0])
        assert capped[-1] is not None  # last pick is a valid, in-bounds sequence

    def test_cap_of_zero_disables(self, monkeypatch):
        monkeypatch.setattr(bootstrap_module, "BOOTSTRAP_MAX_TRAIN_SEQUENCES", 0)
        seqs = self._make(500)
        assert _cap_training_sequences(seqs) is seqs


class TestLoadAndResample:
    def test_skips_malformed_csv_lines(self, tmp_path):
        rows = [
            "timestamp,temperature,humidity,co2,light",
            "2024-01-01 00:00:00,22,65,400,500",
            "2024-01-01 00:05:00,23,66,401,501",
            "2024-01-01 00:10:00,24,67,402,502,extra,fields,here",
        ]
        for i in range(3, 200):
            hour, minute = divmod(i * 5, 60)
            rows.append(f"2024-01-02 {hour:02d}:{minute:02d}:00,22,65,400,500")
        path = tmp_path / "malformed.csv"
        path.write_text("\n".join(rows))

        df = _load_and_resample(str(path))

        assert list(df.columns) == _SENSOR_COLS
        assert len(df) > 0


class TestPass1:
    def test_physics_violation_and_outliers_split_out(self, tmp_path):
        def mutate(df):
            # Impossible T/RH combination → physics flag (HD > 50 g/m³)
            df.iloc[10, df.columns.get_loc("temperature")] = 99.0
            df.iloc[10, df.columns.get_loc("humidity")] = 0.0

        csv = _write_csv(tmp_path / "in.csv", rows=200, mutate=mutate)
        bootstrap = ColdStartBootstrap(
            clean_path=str(tmp_path / "clean.csv"),
            anomaly_path=str(tmp_path / "anomalies.csv"),
        )
        result = bootstrap.run(csv)

        assert result.physics_flags.sum() == 1
        assert len(result.clean_df) + len(result.anomaly_df) == 200
        assert len(result.anomaly_df) >= 1
        # Both output CSVs persisted
        assert (tmp_path / "clean.csv").is_file()
        assert (tmp_path / "anomalies.csv").is_file()

    def test_insufficient_clean_rows_raises(self, tmp_path):
        csv = _write_csv(tmp_path / "small.csv", rows=30)
        bootstrap = ColdStartBootstrap(
            clean_path=str(tmp_path / "clean.csv"),
            anomaly_path=str(tmp_path / "anomalies.csv"),
        )
        with pytest.raises(InsufficientDataError, match="Insufficient clean rows"):
            bootstrap.run(csv)

    def test_physics_skips_out_of_range_humidity(self):
        idx = pd.date_range("2026-06-01", periods=3, freq="5min", tz="UTC")
        df = pd.DataFrame(
            {
                "temperature": [22.0, 22.0, np.nan],
                "humidity": [60.0, 150.0, 60.0],  # 150% RH skipped, NaN temp skipped
                "co2": [420.0] * 3,
                "light": [300.0] * 3,
            },
            index=idx,
        )
        assert _compute_physics_flags(df).sum() == 0

    def test_if_flags_skip_when_too_few_rows(self):
        idx = pd.date_range("2026-06-01", periods=5, freq="5min", tz="UTC")
        df = pd.DataFrame({c: [1.0] * 5 for c in _SENSOR_COLS}, index=idx)
        gas_mask, flags = _compute_if_flags(df)
        assert gas_mask == []
        assert flags.sum() == 0

    def test_gas_columns_masked_when_mostly_null(self):
        idx = pd.date_range("2026-06-01", periods=50, freq="5min", tz="UTC")
        df = pd.DataFrame(
            {
                "temperature": np.linspace(20, 25, 50),
                "humidity": np.linspace(55, 65, 50),
                "co2": [np.nan] * 50,
                "light": np.linspace(100, 300, 50),
            },
            index=idx,
        )
        gas_mask, _ = _compute_if_flags(df)
        assert gas_mask == ["co2"]


class TestPass2:
    def test_train_and_infer_flags_planted_outlier(self, tmp_path):
        clean_csv = _write_csv(tmp_path / "clean.csv", rows=120)
        detector = Pass2BootstrapDetector()
        detector.train(clean_csv)
        assert detector._model is not None

        full_df = pd.read_csv(clean_csv, index_col=0, parse_dates=True)
        results = detector.infer(full_df)
        assert len(results) == 120
        # First SEQ_LEN-1 rows can never be anomalous (no covering sequence)
        assert all(not r.is_anomaly for r in results[: SEQ_LEN - 1])
        assert all(r.reconstruction_loss >= 0.0 for r in results)
        assert all(r.timestamp.endswith("+00:00") for r in results)

    def test_train_skips_tiny_dataset(self, tmp_path):
        clean_csv = _write_csv(tmp_path / "tiny.csv", rows=5)
        detector = Pass2BootstrapDetector()
        detector.train(clean_csv)
        assert detector._model is None

    def test_infer_without_model_returns_all_normal(self, tmp_path):
        detector = Pass2BootstrapDetector()
        full_df = pd.read_csv(
            _write_csv(tmp_path / "full.csv", rows=25), index_col=0, parse_dates=True
        )
        results = detector.infer(full_df)
        assert len(results) == 25
        assert all(not r.is_anomaly for r in results)


class TestHelpers:
    def test_df_to_array_forward_fills_nans(self):
        df = pd.DataFrame(
            {
                "temperature": [20.0, np.nan, 22.0],
                "humidity": [60.0, 61.0, np.nan],
                "co2": [400.0] * 3,
                "light": [100.0] * 3,
            }
        )
        arr = _df_to_array(df)
        assert not np.isnan(arr).any()
        assert arr[1, 0] == pytest.approx(20.0)  # ffilled

    def test_extract_timestamps_non_datetime_index(self):
        df = pd.DataFrame({"temperature": [1.0, 2.0]}, index=["a", "b"])
        assert _extract_timestamps(df) == ["a", "b"]

    def test_classify_root_cause_spike_and_stuck(self):
        n = SEQ_LEN
        data = np.zeros((n, 4), dtype=np.float32)
        data[:, 0] = 20.0
        data[-1, 0] = 40.0  # temperature jump of 20 > SPIKE_DELTA_MAX (5)
        data[:, 1] = 55.0  # humidity constant → stuck
        data[:, 2] = 400.0 + np.arange(n)  # co2 varies gently
        data[:, 3] = np.nan  # light all-NaN → dropout

        tags, sensors = _classify_root_cause(data, n - 1)
        assert "spike" in tags
        assert "stuck" in tags
        assert "dropout" in tags
        assert "temperature" in sensors
        assert "humidity" in sensors
        assert "light" in sensors

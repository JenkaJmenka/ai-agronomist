"""Cadence-guard tests for native greenhouse export fixtures.

These tests verify that fixture files extracted from the native SCADA export
preserve the exact 5-minute cadence, sensor dropout nulls, and ground-truth
anomaly signatures required for reliable integration testing.
"""

from pathlib import Path

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parent
FIXTURE_A = FIXTURES_DIR / "native_dec_stuck_fault.csv"
FIXTURE_B = FIXTURES_DIR / "native_july_clearsky.csv"

HUM_COL = "Measured Relative Humidity (%) [connext2:Compartment 1]"
LIGHT_COL = "Light Intensity (W/m²) [connext2:METEO LIGHT SENSOR]"

CADENCE_SECONDS = 300  # 5-minute native cadence


def _read_raw_fixture(path: Path) -> pd.DataFrame:
    """Read a native-format fixture preserving nulls and original columns."""
    return pd.read_csv(
        str(path),
        sep=";",
        decimal=",",
        encoding="utf-8",
    )


def _parse_timestamps(df: pd.DataFrame) -> pd.Series:
    """Parse the Timestamp column into timezone-naive datetime."""
    return pd.to_datetime(df["Timestamp"], format="%d.%m.%Y %H:%M", dayfirst=True)


# ---------------------------------------------------------------------------
# Fixture A — stuck-fault window (Dec 14 – Dec 26)
# ---------------------------------------------------------------------------


class TestFixtureAStuckFault:
    """Tests for the December stuck-fault fixture."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.df = _read_raw_fixture(FIXTURE_A)
        self.ts = _parse_timestamps(self.df)

    def test_parses_with_csv_adapter(self):
        """Fixture A flows through the real CSVAdapter without error."""
        from src.vendors.csv_adapter import CSVAdapter

        adapter = CSVAdapter(config={"filepath": str(FIXTURE_A)})
        adapter.connect()
        raw = adapter.read()
        normalized = adapter.normalize(raw)
        adapter.disconnect()

        assert not normalized.empty
        assert "timestamp" in normalized.columns
        assert "temperature" in normalized.columns
        assert "humidity" in normalized.columns

    def test_cadence_exactly_300_seconds(self):
        """Every inter-row gap in fixture A is exactly 300 seconds."""
        gaps = self.ts.diff().dt.total_seconds().dropna()
        assert len(gaps) == len(self.df) - 1
        assert (gaps == CADENCE_SECONDS).all(), (
            f"Found non-300s gaps: {sorted(gaps[gaps != CADENCE_SECONDS].unique())}"
        )

    def test_row_count(self):
        """Fixture A has exactly 3,457 data rows (12 days + 1 endpoint at 5-min cadence)."""
        assert len(self.df) == 3457

    def test_stuck_fault_sample_count(self):
        """The main fault window contains exactly 1,897 samples at 100.0.

        Between onset (2026-12-18 13:00) and release (2026-12-25 08:55),
        every non-NaN humidity reading is exactly 100.0, and there are 1,897
        such readings.
        """
        hum = self.df[HUM_COL]
        onset_mask = self.ts == pd.Timestamp("2026-12-18 13:00:00")
        release_mask = self.ts == pd.Timestamp("2026-12-25 08:55:00")

        onset_idx = onset_mask.idxmax()
        release_idx = release_mask.idxmax()

        fault_window = hum.iloc[onset_idx:release_idx]
        count_100 = (fault_window == 100.0).sum()
        assert count_100 == 1897, f"Expected 1897 samples at 100.0, got {count_100}"

        # Every non-NaN value in the fault window must be exactly 100.0
        non_nan = fault_window.dropna()
        assert (non_nan == 100.0).all(), "Found non-100 non-NaN values in fault window"

    def test_stuck_fault_wallclock_duration(self):
        """Wall-clock span from onset to last 100.0 is 163.83 hours."""
        hum = self.df[HUM_COL]
        onset_ts = pd.Timestamp("2026-12-18 13:00:00")
        onset_mask = self.ts == onset_ts
        onset_idx = onset_mask.idxmax()

        # Find last 100.0 before release
        release_mask = self.ts == pd.Timestamp("2026-12-25 08:55:00")
        release_idx = release_mask.idxmax()

        # Walk backwards from release to find last 100.0
        last_100_idx = release_idx - 1
        while pd.isna(hum.iloc[last_100_idx]):
            last_100_idx -= 1
        assert hum.iloc[last_100_idx] == 100.0

        last_100_ts = self.ts.iloc[last_100_idx]
        duration_h = (last_100_ts - onset_ts).total_seconds() / 3600
        assert round(duration_h, 2) == 163.83, (
            f"Expected 163.83h, got {round(duration_h, 2)}h"
        )

    def test_freeze_onset_timestamp(self):
        """Fault onset is at exactly 2026-12-18T13:00:00."""
        hum = self.df[HUM_COL]
        onset_mask = self.ts == pd.Timestamp("2026-12-18 13:00:00")
        assert onset_mask.any(), "Onset timestamp not found in fixture"

        onset_idx = onset_mask.idxmax()
        assert hum.iloc[onset_idx] == 100.0, "Value at onset is not 100.0"

        # Verify immediately before onset is NOT 100.0
        pre_onset_idx = onset_idx - 1
        assert hum.iloc[pre_onset_idx] != 100.0, (
            f"Sample before onset is also 100.0 — onset boundary is wrong"
        )

    def test_pre_onset_values(self):
        """Pre-onset humidity readings match expected near-saturation values."""
        onset_mask = self.ts == pd.Timestamp("2026-12-18 13:00:00")
        onset_idx = onset_mask.idxmax()

        expected = [
            ("2026-12-18 12:35", 99.17),
            ("2026-12-18 12:40", 99.00),
            ("2026-12-18 12:45", 99.00),
            ("2026-12-18 12:50", 99.00),
            ("2026-12-18 12:55", 99.83),
        ]
        for ts_str, expected_val in expected:
            mask = self.ts == pd.Timestamp(ts_str)
            assert mask.any(), f"Timestamp {ts_str} not found"
            idx = mask.idxmax()
            actual = self.df[HUM_COL].iloc[idx]
            assert actual == expected_val, (
                f"At {ts_str}: expected {expected_val}, got {actual}"
            )

    def test_null_humidity_preserved(self):
        """Fixture A preserves exactly 70 null humidity readings (sensor dropouts)."""
        null_count = self.df[HUM_COL].isna().sum()
        assert null_count == 70, (
            f"Expected 70 null humidity readings, got {null_count}"
        )


# ---------------------------------------------------------------------------
# Fixture B — clear-sky July week (Jul 1 – Jul 7)
# ---------------------------------------------------------------------------


class TestFixtureBClearSky:
    """Tests for the July clear-sky fixture."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.df = _read_raw_fixture(FIXTURE_B)
        self.ts = _parse_timestamps(self.df)

    def test_parses_with_csv_adapter(self):
        """Fixture B flows through the real CSVAdapter without error."""
        from src.vendors.csv_adapter import CSVAdapter

        adapter = CSVAdapter(config={"filepath": str(FIXTURE_B)})
        adapter.connect()
        raw = adapter.read()
        normalized = adapter.normalize(raw)
        adapter.disconnect()

        assert not normalized.empty
        assert "timestamp" in normalized.columns
        assert "light" in normalized.columns

    def test_cadence_exactly_300_seconds(self):
        """Every inter-row gap in fixture B is exactly 300 seconds."""
        gaps = self.ts.diff().dt.total_seconds().dropna()
        assert len(gaps) == len(self.df) - 1
        assert (gaps == CADENCE_SECONDS).all(), (
            f"Found non-300s gaps: {sorted(gaps[gaps != CADENCE_SECONDS].unique())}"
        )

    def test_row_count(self):
        """Fixture B has exactly 2,016 data rows (7 days at 5-min cadence)."""
        assert len(self.df) == 2016

    def test_max_light_above_900(self):
        """Maximum light intensity in fixture B exceeds 900 W/m2."""
        max_light = self.df[LIGHT_COL].max()
        assert max_light > 900, f"Max light {max_light} is not > 900"

    def test_max_light_value(self):
        """Maximum light intensity is 1019.98 W/m2 (measured ground truth)."""
        max_light = self.df[LIGHT_COL].max()
        assert max_light == pytest.approx(1019.98, abs=0.01), (
            f"Expected max light ~1019.98, got {max_light}"
        )

    def test_null_humidity_count(self):
        """Fixture B has zero null humidity readings (verified from source)."""
        null_count = self.df[HUM_COL].isna().sum()
        assert null_count == 0, (
            f"Expected 0 null humidity readings, got {null_count}"
        )

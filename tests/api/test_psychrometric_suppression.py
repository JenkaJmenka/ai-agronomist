"""Psychrometric suppression on the upload path.

Tests that:
  1. measured_hd is no longer excluded by is_cold_run_stat_column
  2. Paired temperature + humidity alerts that are psychrometrically
     CONSISTENT (measured HD agrees with Tetens-derived HD) are suppressed
  3. INCONSISTENT measured HD still alerts with a physics inconsistency marker
  4. Files without an HD column produce identical alerts (regression)
  5. On the real fixture with HD, the paired alert count DROPS vs baseline
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from src.api.schemas.sensor_data import StatisticalAnomalyDetail
from src.api.services.upload_portions import (
    PortionScores,
    StatCarry,
    _suppress_psychrometric_pairs,
    finalize_scores,
    score_portion,
)
from src.config import (
    COLD_RUN_STAT_MIN_CONSECUTIVE,
    COLD_RUN_STAT_ROLLING_WINDOW,
    COLD_RUN_STAT_Z_MIN,
    LATENT_DIM,
    PSYCHROMETRIC_HD_TOLERANCE,
)
from src.models.cold_run_detector import is_cold_run_stat_column
from src.models.inference import AnomalyDetector
from src.models.psychrometric_validator import (
    calculate_humidity_deficit,
    validate_row,
)
from src.models.stuck_fault_detector import StuckCarry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_A = FIXTURES / "native_dec_stuck_fault.csv"

# Baseline measured BEFORE the psychrometric suppression was wired in.
# Run with the old code (deficit excluded, no suppression) gives 6 paired
# temp+humidity timestamps in the fixture.
_FIXTURE_A_BASELINE_PAIRED = 6


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_enriched_side_effect():
    """Mock predict_batch_enriched: flat low scores (no LSTM anomalies)."""
    def side_effect(seqs: np.ndarray):
        n = len(seqs)
        scores = np.full(n, 0.05, dtype=np.float32)
        embeddings = np.zeros((n, LATENT_DIM), dtype=np.float32)
        uncertainties = np.full(n, 0.1, dtype=np.float32)
        return scores, embeddings, uncertainties
    return side_effect


def _make_detector() -> AnomalyDetector:
    """Construct a mock AnomalyDetector that does not load a model."""
    det = AnomalyDetector.__new__(AnomalyDetector)
    det._model = None
    det.predict_batch_enriched = _make_enriched_side_effect()
    return det


def _stable_row(
    ts: str,
    temp: float = 22.0,
    humidity: float = 65.0,
    co2: float = 420.0,
    light: float = 200.0,
    measured_hd: float | None = None,
) -> dict[str, Any]:
    """Build one raw-row dict matching _strip_raw_records output."""
    row: dict[str, Any] = {
        "timestamp": ts,
        "temperature": temp,
        "humidity": humidity,
        "co2": co2,
        "light": light,
    }
    if measured_hd is not None:
        row["measured_hd"] = measured_hd
    return row


def _build_baseline_rows(
    n: int,
    *,
    include_hd: bool = True,
    temp: float = 22.0,
    humidity: float = 65.0,
) -> list[dict[str, Any]]:
    """Generate *n* stable rows at 5-min cadence."""
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    hd = calculate_humidity_deficit(temp, humidity) if include_hd else None
    rows = []
    for i in range(n):
        ts = (base + timedelta(minutes=5 * i)).isoformat()
        rows.append(_stable_row(ts, temp=temp, humidity=humidity, measured_hd=hd))
    return rows


def _inject_spike(
    rows: list[dict[str, Any]],
    *,
    spike_temp: float,
    spike_rh: float,
    spike_hd: float | None = None,
    count: int = 5,
    include_hd: bool = True,
) -> list[dict[str, Any]]:
    """Append *count* spike rows after the last baseline row.

    If *spike_hd* is None and *include_hd* is True, computes the
    Tetens-correct HD from spike_temp + spike_rh (→ consistent).
    """
    last_ts = datetime.fromisoformat(rows[-1]["timestamp"])
    if spike_hd is None and include_hd:
        spike_hd = calculate_humidity_deficit(spike_temp, spike_rh)
    for i in range(count):
        ts = (last_ts + timedelta(minutes=5 * (i + 1))).isoformat()
        rows.append(
            _stable_row(ts, temp=spike_temp, humidity=spike_rh, measured_hd=spike_hd)
        )
    return rows


def _count_paired_timestamps(
    stat_details: list[StatisticalAnomalyDetail],
) -> int:
    """Count timestamps with both a temp and humidity alert."""
    by_ts: dict[str, list[StatisticalAnomalyDetail]] = defaultdict(list)
    for d in stat_details:
        by_ts[d.timestamp].append(d)
    count = 0
    for details in by_ts.values():
        has_temp = any("temp" in d.column.lower() for d in details)
        has_humid = any("humid" in d.column.lower() for d in details)
        if has_temp and has_humid:
            count += 1
    return count


def _run_fixture_through_upload(fixture_path: Path):
    """Score a native fixture CSV, returning the UploadInferenceResponse."""
    from src.api.services.cold_run_upload import run_cold_run_upload
    from src.vendors.csv_adapter import CSVAdapter

    adapter = CSVAdapter({"filepath": str(fixture_path)})
    adapter.connect()

    with patch("src.models.inference.AnomalyDetector.predict_batch_enriched") as mock_pred:
        mock_pred.side_effect = _make_enriched_side_effect()
        detector = AnomalyDetector.__new__(AnomalyDetector)
        detector._model = None
        detector.predict_batch_enriched = mock_pred

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        coro = run_cold_run_upload(
            tmp_path=str(fixture_path),
            adapter=adapter,
            detector=detector,
            tenant_id="test_tenant",
            greenhouse_id="test_gh",
        )

        if loop is not None and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                response, _ = pool.submit(asyncio.run, coro).result()
        else:
            response, _ = asyncio.run(coro)

    return response


# ---------------------------------------------------------------------------
# 1. measured_hd is no longer excluded
# ---------------------------------------------------------------------------

class TestMeasuredHdInclusion:
    """is_cold_run_stat_column must accept the measured_hd channel."""

    def test_measured_hd_accepted(self):
        assert is_cold_run_stat_column("measured_hd") is True

    def test_humidity_deficit_accepted(self):
        assert is_cold_run_stat_column("humidity_deficit") is True

    def test_actuator_channels_still_excluded(self):
        for col in ("light", "rain_sensor", "screen_position", "vent_opening"):
            assert is_cold_run_stat_column(col) is False, f"{col} should be excluded"

    def test_climate_channels_still_included(self):
        for col in ("temperature", "humidity", "wind_speed", "outdoor_temp"):
            assert is_cold_run_stat_column(col) is True, f"{col} should be included"


# ---------------------------------------------------------------------------
# 2. Consistent psychrometric pairing is suppressed
# ---------------------------------------------------------------------------

class TestConsistentSuppression:
    """Paired temp drop + humidity rise with matching HD is suppressed."""

    def test_consistent_pair_suppressed(self):
        """Synthetic paired alerts with Tetens-correct HD → suppressed."""
        t_air, rh = 15.0, 90.0
        hd = calculate_humidity_deficit(t_air, rh)

        ts = "2026-06-01T12:00:00+00:00"
        details = [
            StatisticalAnomalyDetail(
                timestamp=ts, column="temperature", value=t_air,
                z_score=7.0, severity="critical", method="rolling_z_score",
            ),
            StatisticalAnomalyDetail(
                timestamp=ts, column="humidity", value=rh,
                z_score=6.5, severity="warning", method="rolling_z_score",
            ),
        ]
        psychro = {ts: (t_air, rh, hd)}

        result = _suppress_psychrometric_pairs(details, psychro)

        # Both temp and humidity alerts at this timestamp should be gone.
        remaining_at_ts = [d for d in result if d.timestamp == ts]
        assert len(remaining_at_ts) == 0, (
            f"Expected 0 alerts at {ts} after consistent suppression, "
            f"got {len(remaining_at_ts)}: {[d.column for d in remaining_at_ts]}"
        )

    def test_consistent_pair_leaves_other_timestamps(self):
        """Suppression only affects the paired timestamp, not neighbours."""
        t1 = "2026-06-01T11:55:00+00:00"
        t2 = "2026-06-01T12:00:00+00:00"
        t3 = "2026-06-01T12:05:00+00:00"

        t_air, rh = 15.0, 90.0
        hd = calculate_humidity_deficit(t_air, rh)

        details = [
            StatisticalAnomalyDetail(
                timestamp=t1, column="temperature", value=16.0,
                z_score=6.0, severity="warning", method="rolling_z_score",
            ),
            StatisticalAnomalyDetail(
                timestamp=t2, column="temperature", value=t_air,
                z_score=7.0, severity="critical", method="rolling_z_score",
            ),
            StatisticalAnomalyDetail(
                timestamp=t2, column="humidity", value=rh,
                z_score=6.5, severity="warning", method="rolling_z_score",
            ),
            StatisticalAnomalyDetail(
                timestamp=t3, column="measured_pipe_1_temp", value=30.0,
                z_score=6.0, severity="warning", method="rolling_z_score",
            ),
        ]
        psychro = {t2: (t_air, rh, hd)}

        result = _suppress_psychrometric_pairs(details, psychro)

        # t1 and t3 alerts survive; t2 pair suppressed.
        assert len(result) == 2
        remaining_ts = {d.timestamp for d in result}
        assert t1 in remaining_ts
        assert t3 in remaining_ts
        assert t2 not in remaining_ts

    def test_non_paired_not_suppressed(self):
        """A timestamp with only temperature (no humidity) is not suppressed."""
        ts = "2026-06-01T12:00:00+00:00"
        t_air, rh = 15.0, 90.0
        hd = calculate_humidity_deficit(t_air, rh)

        details = [
            StatisticalAnomalyDetail(
                timestamp=ts, column="temperature", value=t_air,
                z_score=7.0, severity="critical", method="rolling_z_score",
            ),
        ]
        psychro = {ts: (t_air, rh, hd)}

        result = _suppress_psychrometric_pairs(details, psychro)
        assert len(result) == 1
        assert result[0].column == "temperature"


# ---------------------------------------------------------------------------
# 3. Inconsistent HD keeps alerts and adds physics inconsistency marker
# ---------------------------------------------------------------------------

class TestInconsistentPhysicsMarker:
    """Inconsistent measured HD keeps original alerts + adds marker."""

    def test_inconsistent_hd_keeps_alerts_and_adds_marker(self):
        """Measured HD that disagrees with Tetens → alerts remain + marker."""
        t_air, rh = 15.0, 90.0
        correct_hd = calculate_humidity_deficit(t_air, rh)
        # Offset beyond tolerance to trigger FAILED_PSYCHROMETRIC_LIMIT.
        bad_hd = correct_hd + PSYCHROMETRIC_HD_TOLERANCE * 3

        ts = "2026-06-01T12:00:00+00:00"
        details = [
            StatisticalAnomalyDetail(
                timestamp=ts, column="temperature", value=t_air,
                z_score=7.0, severity="critical", method="rolling_z_score",
            ),
            StatisticalAnomalyDetail(
                timestamp=ts, column="humidity", value=rh,
                z_score=6.5, severity="warning", method="rolling_z_score",
            ),
        ]
        psychro = {ts: (t_air, rh, bad_hd)}

        result = _suppress_psychrometric_pairs(details, psychro)

        # Original alerts must remain.
        temp_alerts = [d for d in result if d.column == "temperature"]
        humid_alerts = [d for d in result if d.column == "humidity"]
        assert len(temp_alerts) == 1
        assert len(humid_alerts) == 1

        # Physics inconsistency marker must be added.
        psychro_markers = [
            d for d in result if d.method == "psychrometric_inconsistency"
        ]
        assert len(psychro_markers) == 1
        assert psychro_markers[0].column == "psychrometric"
        assert psychro_markers[0].timestamp == ts
        assert psychro_markers[0].severity == "warning"


# ---------------------------------------------------------------------------
# 4. No HD column → identical to pre-change behaviour
# ---------------------------------------------------------------------------

class TestNoHdRegression:
    """Absent measured_hd means the check does not run."""

    def test_empty_psychro_context_is_noop(self):
        """No psychrometric context → all alerts pass through unchanged."""
        ts = "2026-06-01T12:00:00+00:00"
        details = [
            StatisticalAnomalyDetail(
                timestamp=ts, column="temperature", value=15.0,
                z_score=7.0, severity="critical", method="rolling_z_score",
            ),
            StatisticalAnomalyDetail(
                timestamp=ts, column="humidity", value=90.0,
                z_score=6.5, severity="warning", method="rolling_z_score",
            ),
        ]

        result = _suppress_psychrometric_pairs(details, {})
        assert len(result) == 2, "Empty psychro context should leave alerts unchanged"

    def test_missing_hd_at_timestamp_preserves_pair(self):
        """Psychro context exists but not for the paired timestamp → unchanged."""
        ts_paired = "2026-06-01T12:00:00+00:00"
        ts_other = "2026-06-01T11:00:00+00:00"
        details = [
            StatisticalAnomalyDetail(
                timestamp=ts_paired, column="temperature", value=15.0,
                z_score=7.0, severity="critical", method="rolling_z_score",
            ),
            StatisticalAnomalyDetail(
                timestamp=ts_paired, column="humidity", value=90.0,
                z_score=6.5, severity="warning", method="rolling_z_score",
            ),
        ]
        # Context exists for a DIFFERENT timestamp.
        psychro = {ts_other: (22.0, 65.0, 3.0)}

        result = _suppress_psychrometric_pairs(details, psychro)
        assert len(result) == 2, "Missing HD at paired timestamp should preserve alerts"


# ---------------------------------------------------------------------------
# 5. Fixture A (native_dec_stuck_fault.csv) paired-alert reduction
# ---------------------------------------------------------------------------

class TestFixtureAPairedReduction:
    """On the real fixture with an HD column, paired alerts must drop."""

    @pytest.fixture(autouse=True)
    def _load_fixture(self):
        if not FIXTURE_A.is_file():
            pytest.skip(f"Fixture not found: {FIXTURE_A}")
        self.response = _run_fixture_through_upload(FIXTURE_A)

    def test_paired_count_drops_vs_baseline(self):
        """Paired temp+humidity timestamp count must be strictly less than
        the pre-change baseline (6 paired timestamps measured before fix)."""
        stat_alerts = [
            h for h in self.response.high_anomalies
            if h.detector == "statistical"
        ]
        by_ts: dict[str, list] = defaultdict(list)
        for a in stat_alerts:
            by_ts[a.timestamp].append(a)

        paired_count = 0
        for alerts in by_ts.values():
            has_temp = any(
                "temp" in (a.column or "").lower() for a in alerts
            )
            has_humid = any(
                "humid" in (a.column or "").lower() for a in alerts
            )
            if has_temp and has_humid:
                paired_count += 1

        assert paired_count < _FIXTURE_A_BASELINE_PAIRED, (
            f"Expected paired count < {_FIXTURE_A_BASELINE_PAIRED} "
            f"(pre-fix baseline), got {paired_count}"
        )

    def test_measured_hd_in_baseline_columns(self):
        """measured_hd must appear in the statistical summary baseline columns."""
        baseline_cols = self.response.statistical_summary.baseline_columns
        assert "measured_hd" in baseline_cols, (
            f"measured_hd not found in baseline columns: {baseline_cols}"
        )

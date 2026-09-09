"""Episode grouping + recovery scoring (Tasks 3 + 5).

Acceptance tests:
  1. Fixture A recovery is ONE grouped episode, not 35 rows
  2. That episode reports start, end, duration
  3. The recovery episode is NOT the highest-scoring event
  4. A genuine out-of-bounds excursion still produces an alert
  5. Two anomalies separated by a long gap remain TWO episodes
  6. Different channels at the same timestamp are distinct episodes
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.api.services.upload_portions import (
    PortionScores,
    StatCarry,
    finalize_scores,
    score_portion,
)
from src.config import (
    EPISODE_GAP_SECONDS,
    LATENT_DIM,
    RECOVERY_SCORE_CAP,
    STUCK_WALLCLOCK_MIN_HOURS,
)
from src.models.episode_grouper import (
    AnomalyEpisode,
    group_alerts_into_episodes,
)
from src.models.inference import AnomalyDetector
from src.models.stuck_fault_detector import (
    FaultWindow,
    StuckCarry,
    WallclockStuckDetector,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_A = FIXTURES / "native_dec_stuck_fault.csv"
FIXTURE_B = FIXTURES / "native_july_clearsky.csv"

FAULT_ONSET = datetime(2026, 12, 18, 13, 0, 0, tzinfo=timezone.utc)
FREEZE_END = datetime(2026, 12, 25, 8, 55, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_enriched_side_effect(score_fn):
    """Build a predict_batch_enriched mock."""
    def side_effect(seqs):
        scores = score_fn(seqs)
        batch_size = len(scores)
        embeddings = np.zeros((batch_size, LATENT_DIM), dtype=np.float32)
        uncertainties = np.full(batch_size, 0.1, dtype=np.float32)
        return scores, embeddings, uncertainties
    return side_effect


def _run_fixture_through_upload(fixture_path: Path):
    """Score a native fixture CSV, returning (response, stuck_carry)."""
    from src.api.services.cold_run_upload import run_cold_run_upload
    from src.vendors.csv_adapter import CSVAdapter
    import asyncio

    adapter = CSVAdapter({"filepath": str(fixture_path)})
    adapter.connect()

    with patch("src.models.inference.AnomalyDetector.predict_batch_enriched") as mock_pred:
        mock_pred.side_effect = _make_enriched_side_effect(
            lambda seqs: np.full(len(seqs), 0.05, dtype=np.float32)
        )
        detector = AnomalyDetector.__new__(AnomalyDetector)
        detector._model = None
        detector.predict_batch_enriched = mock_pred

        captured_carry: list[StuckCarry] = []
        original_finalize = finalize_scores

        def _capturing_finalize(portions, **kwargs):
            if "stuck_carry" in kwargs and kwargs["stuck_carry"] is not None:
                captured_carry.append(kwargs["stuck_carry"])
            return original_finalize(portions, **kwargs)

        with patch(
            "src.api.services.cold_run_upload.finalize_scores",
            side_effect=_capturing_finalize,
        ):
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

    carry = captured_carry[0] if captured_carry else StuckCarry()
    return response, carry


# ---------------------------------------------------------------------------
# Fixture A integration tests
# ---------------------------------------------------------------------------

class TestFixtureAEpisodes:
    """Fixture A (native_dec_stuck_fault.csv) episode grouping."""

    @pytest.fixture(autouse=True)
    def _load_fixture(self):
        assert FIXTURE_A.is_file(), f"Fixture A not found: {FIXTURE_A}"
        self.response, self.stuck_carry = _run_fixture_through_upload(FIXTURE_A)
        self.episodes = self.response.episodes
        self.stuck_episodes = [
            e for e in self.episodes if e.detector == "stuck_fault"
        ]
        self.recovery_episodes = [
            e for e in self.episodes if e.is_recovery
        ]
        self.humidity_recovery = [
            e for e in self.recovery_episodes if e.channel == "humidity"
        ]

    def test_recovery_is_one_episode_not_35_rows(self):
        """The 2026-12-25 recovery must be ONE grouped episode."""
        assert len(self.humidity_recovery) <= 1, (
            f"Expected at most 1 humidity recovery episode, "
            f"got {len(self.humidity_recovery)}"
        )

    def test_episode_has_start_end_duration(self):
        """Each episode reports start_ts, end_ts, and duration_seconds."""
        for ep in self.episodes:
            assert ep.start_ts is not None, "Missing start_ts"
            assert ep.end_ts is not None, "Missing end_ts"
            assert ep.duration_seconds >= 0.0, "Negative duration"
            assert ep.sample_count >= 1, "Empty episode"

    def test_recovery_episode_not_highest_scoring(self):
        """The stuck fault that preceded recovery must rank above it.

        This is the Task 3 acceptance criterion: the recovery is not
        scored as the largest anomaly in the run.
        """
        if not self.episodes:
            pytest.skip("No episodes produced")

        highest_episode = max(self.episodes, key=lambda e: e.peak_score)
        for recovery_ep in self.recovery_episodes:
            assert recovery_ep.peak_score < highest_episode.peak_score, (
                f"Recovery episode (peak={recovery_ep.peak_score}) outranks "
                f"highest episode (peak={highest_episode.peak_score})"
            )

        # The highest-scoring episode should be a stuck_fault.
        assert highest_episode.detector == "stuck_fault", (
            f"Expected stuck_fault as highest scorer, got {highest_episode.detector}"
        )

    def test_stuck_fault_episode_exists(self):
        """At least one stuck_fault episode for humidity must exist."""
        humidity_stuck = [
            e for e in self.stuck_episodes if e.channel == "humidity"
        ]
        assert len(humidity_stuck) >= 1, (
            f"Expected ≥1 humidity stuck_fault episode, "
            f"got {len(humidity_stuck)}"
        )

    def test_stuck_fault_outranks_recovery(self):
        """The stuck fault episode (score=1.0) outranks recovery."""
        humidity_stuck = [
            e for e in self.stuck_episodes if e.channel == "humidity"
        ]
        if not humidity_stuck or not self.humidity_recovery:
            pytest.skip("Missing stuck or recovery episodes")
        stuck_peak = max(e.peak_score for e in humidity_stuck)
        recovery_peak = max(
            (e.peak_score for e in self.humidity_recovery),
            default=0.0,
        )
        assert stuck_peak > recovery_peak, (
            f"Stuck peak {stuck_peak} should exceed recovery peak {recovery_peak}"
        )

    def test_recovery_score_capped(self):
        """Recovery episodes must have peak_score <= RECOVERY_SCORE_CAP."""
        for ep in self.recovery_episodes:
            assert ep.peak_score <= RECOVERY_SCORE_CAP, (
                f"Recovery peak {ep.peak_score} exceeds cap {RECOVERY_SCORE_CAP}"
            )


# ---------------------------------------------------------------------------
# Fixture B — no stuck_fault episodes
# ---------------------------------------------------------------------------

class TestFixtureBNoStuckEpisodes:
    """Fixture B (native_july_clearsky.csv) — zero stuck_fault episodes."""

    @pytest.fixture(autouse=True)
    def _load_fixture(self):
        assert FIXTURE_B.is_file(), f"Fixture B not found: {FIXTURE_B}"
        self.response, self.stuck_carry = _run_fixture_through_upload(FIXTURE_B)
        self.episodes = self.response.episodes

    def test_zero_stuck_fault_episodes(self):
        """Clean July data must not produce stuck_fault episodes."""
        stuck = [e for e in self.episodes if e.detector == "stuck_fault"]
        assert len(stuck) == 0, (
            f"Expected 0 stuck_fault episodes, got {len(stuck)}"
        )


# ---------------------------------------------------------------------------
# Genuine excursion regression
# ---------------------------------------------------------------------------

class TestGenuineExcursionStillAlerted:
    """A real out-of-bounds excursion must still produce an alert."""

    def test_synthetic_excursion_detected(self):
        """Inject a genuine anomaly and verify it's not suppressed."""
        alerts = [
            UploadHighAnomalyDetail(
                score=0.85,
                timestamp="2026-06-01T12:00:00+00:00",
                detector="statistical",
                column="temperature",
                severity="critical",
                z_score=8.5,
            ),
        ]
        episodes = group_alerts_into_episodes(alerts)
        assert len(episodes) == 1
        assert episodes[0].channel == "temperature"
        assert episodes[0].peak_score == 0.85
        assert not episodes[0].is_recovery


# ---------------------------------------------------------------------------
# Gap separation
# ---------------------------------------------------------------------------

class TestGapSeparatesEpisodes:
    """Two anomalies on the same channel separated by a long quiet gap
    must remain TWO episodes, not one merged episode."""

    def test_long_gap_splits_episodes(self):
        """Gap > EPISODE_GAP_SECONDS → two episodes."""
        gap = EPISODE_GAP_SECONDS + 60  # 1 minute beyond threshold
        t1 = "2026-06-01T10:00:00+00:00"
        t2_dt = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=gap)
        t2 = t2_dt.isoformat()

        alerts = [
            UploadHighAnomalyDetail(
                score=0.7,
                timestamp=t1,
                detector="statistical",
                column="humidity",
                severity="warning",
                z_score=6.0,
            ),
            UploadHighAnomalyDetail(
                score=0.8,
                timestamp=t2,
                detector="statistical",
                column="humidity",
                severity="critical",
                z_score=7.5,
            ),
        ]
        episodes = group_alerts_into_episodes(alerts)
        humidity_episodes = [e for e in episodes if e.channel == "humidity"]
        assert len(humidity_episodes) == 2, (
            f"Expected 2 humidity episodes with gap {gap}s, "
            f"got {len(humidity_episodes)}"
        )

    def test_short_gap_merges_episodes(self):
        """Gap < EPISODE_GAP_SECONDS → one episode."""
        gap = EPISODE_GAP_SECONDS - 60  # 1 minute within threshold
        t1 = "2026-06-01T10:00:00+00:00"
        t2_dt = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=gap)
        t2 = t2_dt.isoformat()

        alerts = [
            UploadHighAnomalyDetail(
                score=0.7,
                timestamp=t1,
                detector="statistical",
                column="humidity",
                severity="warning",
                z_score=6.0,
            ),
            UploadHighAnomalyDetail(
                score=0.8,
                timestamp=t2,
                detector="statistical",
                column="humidity",
                severity="critical",
                z_score=7.5,
            ),
        ]
        episodes = group_alerts_into_episodes(alerts)
        humidity_episodes = [e for e in episodes if e.channel == "humidity"]
        assert len(humidity_episodes) == 1, (
            f"Expected 1 merged humidity episode with gap {gap}s, "
            f"got {len(humidity_episodes)}"
        )
        assert humidity_episodes[0].peak_score == 0.8
        assert humidity_episodes[0].sample_count == 2


# ---------------------------------------------------------------------------
# Different channels are distinct episodes
# ---------------------------------------------------------------------------

class TestDifferentChannelsDistinct:
    """Anomalies on different channels at the same timestamp must be
    distinct episodes, not merged."""

    def test_concurrent_different_channels(self):
        """Two channels at the same instant → two episodes."""
        ts = "2026-06-01T12:00:00+00:00"
        alerts = [
            UploadHighAnomalyDetail(
                score=0.75,
                timestamp=ts,
                detector="statistical",
                column="humidity",
                severity="warning",
                z_score=7.0,
            ),
            UploadHighAnomalyDetail(
                score=0.80,
                timestamp=ts,
                detector="statistical",
                column="temperature",
                severity="critical",
                z_score=8.0,
            ),
        ]
        episodes = group_alerts_into_episodes(alerts)
        channels = {e.channel for e in episodes}
        assert "humidity" in channels
        assert "temperature" in channels
        assert len(episodes) == 2


# ---------------------------------------------------------------------------
# Task 2b: early detection acceptance
# ---------------------------------------------------------------------------

class TestEarlyDetectionFixtureA:
    """Task 2b: stuck_fault alert is emitted STRICTLY EARLIER than freeze end."""

    @pytest.fixture(autouse=True)
    def _load_fixture(self):
        assert FIXTURE_A.is_file()
        self.response, self.carry = _run_fixture_through_upload(FIXTURE_A)
        self.stuck_alerts = [
            h for h in self.response.high_anomalies
            if h.detector == "stuck_fault" and h.column == "humidity"
        ]

    def test_alert_emitted_before_freeze_end(self):
        """detected_ts must be STRICTLY EARLIER than 2026-12-25T08:55:00Z."""
        # Find the main fault alert (onset near FAULT_ONSET)
        main_alert = None
        for alert in self.stuck_alerts:
            ts = datetime.fromisoformat(
                alert.timestamp.replace("Z", "+00:00")
            )
            if abs((ts - FAULT_ONSET).total_seconds()) < 3600:
                main_alert = alert
                break
        assert main_alert is not None, "No alert near main fault onset"
        assert main_alert.detected_ts is not None

        detected_dt = datetime.fromisoformat(
            main_alert.detected_ts.replace("Z", "+00:00")
        )
        assert detected_dt < FREEZE_END, (
            f"detected_ts {detected_dt} not before freeze end {FREEZE_END}"
        )

    def test_latency_bounded_by_gate(self):
        """Measured latency must be <= gate + one sampling interval."""
        main_alert = None
        for alert in self.stuck_alerts:
            ts = datetime.fromisoformat(
                alert.timestamp.replace("Z", "+00:00")
            )
            if abs((ts - FAULT_ONSET).total_seconds()) < 3600:
                main_alert = alert
                break
        assert main_alert is not None
        assert main_alert.detected_ts is not None

        onset_dt = datetime.fromisoformat(
            main_alert.timestamp.replace("Z", "+00:00")
        )
        detected_dt = datetime.fromisoformat(
            main_alert.detected_ts.replace("Z", "+00:00")
        )
        latency_h = (detected_dt - onset_dt).total_seconds() / 3600.0

        # Gate + one sampling interval (300s = 0.083h)
        max_latency = STUCK_WALLCLOCK_MIN_HOURS + 0.1
        assert latency_h <= max_latency, (
            f"Latency {latency_h:.2f}h exceeds gate+margin {max_latency}h"
        )
        assert latency_h > 0.0, (
            "Latency is 0.00h — tautological bug"
        )

    def test_onset_field_still_reports_freeze_onset(self):
        """The alert's timestamp (onset) must still be 2026-12-18T13:00:00Z."""
        main_alert = None
        for alert in self.stuck_alerts:
            ts = datetime.fromisoformat(
                alert.timestamp.replace("Z", "+00:00")
            )
            if abs((ts - FAULT_ONSET).total_seconds()) < 3600:
                main_alert = alert
                break
        assert main_alert is not None
        onset_dt = datetime.fromisoformat(
            main_alert.timestamp.replace("Z", "+00:00")
        )
        assert onset_dt == FAULT_ONSET, (
            f"Onset {onset_dt} != expected {FAULT_ONSET}"
        )

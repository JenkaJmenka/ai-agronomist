"""Stuck-fault / flatline detection on the CSV upload path.

Acceptance tests for wall-clock-gated stuck detection:
  - fixture A (native_dec_stuck_fault.csv) → at least one stuck_fault/urgent
  - fixture B (native_july_clearsky.csv)   → zero stuck_fault alerts
  - zero-variance window → routed to stuck path, not dropped by rolling_std guard
  - 15-minute cadence → cadence flag fires
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.api.services.upload_portions import (
    PortionScores,
    StatCarry,
    finalize_scores,
    score_portion,
)
from src.config import (
    CADENCE_EXPECTED_SECONDS,
    LATENT_DIM,
    STUCK_WALLCLOCK_MIN_HOURS,
    STUCK_WALLCLOCK_MIN_SAMPLES,
)
from src.models.inference import AnomalyDetector
from src.models.stuck_fault_detector import (
    StuckCarry,
    WallclockStuckDetector,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FIXTURE_A = FIXTURES / "native_dec_stuck_fault.csv"
FIXTURE_B = FIXTURES / "native_july_clearsky.csv"

FAULT_ONSET = datetime(2026, 12, 18, 13, 0, 0, tzinfo=timezone.utc)
FAULT_DURATION_HOURS = 163.83

# Fixture A ground truth (tests/fixtures/NATIVE_FIXTURES.md:31 onward): five
# humidity freeze runs at 100.0, of which exactly FOUR cross the 6 h gate.
# Run #4 (1.00 h) is the only discriminating case here — "four alerts" and
# "6.00 h frozen at detection" are one fact stated twice, not two independent
# checks, because the gate stamps detected_ts = onset + STUCK_WALLCLOCK_MIN_HOURS
# and therefore fixes the count and the duration together.
EXPECTED_HUMIDITY_ALERT_COUNT = 4


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_enriched_side_effect(score_fn):
    """Build a predict_batch_enriched mock returning scores + dummy embeddings."""
    def side_effect(seqs):
        scores = score_fn(seqs)
        batch_size = len(scores)
        embeddings = np.zeros((batch_size, LATENT_DIM), dtype=np.float32)
        uncertainties = np.full(batch_size, 0.1, dtype=np.float32)
        return scores, embeddings, uncertainties
    return side_effect


def _build_constant_df(
    *,
    channel: str = "humidity",
    value: float = 100.0,
    n_rows: int = 200,
    start: datetime | None = None,
    cadence_seconds: int = 300,
) -> pd.DataFrame:
    """Synthetic normalised DataFrame with one channel constant."""
    if start is None:
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    timestamps = [start + timedelta(seconds=i * cadence_seconds) for i in range(n_rows)]
    data: dict[str, list[float] | list[datetime]] = {
        "timestamp": timestamps,
        "temperature": [20.0 + 0.1 * (i % 50) for i in range(n_rows)],
        "humidity": [65.0 + 0.5 * (i % 30) for i in range(n_rows)],
        "co2": [400.0 + (i % 20) for i in range(n_rows)],
        "light": [0.0] * n_rows,
    }
    data[channel] = [value] * n_rows
    return pd.DataFrame(data)


def _dict_to_detail(alert: dict) -> UploadHighAnomalyDetail:
    """Convert a WallclockStuckDetector alert dict to UploadHighAnomalyDetail.

    Mirrors production ``upload_portions.py::_stuck_dict_to_high_detail``: the
    gate-crossing duration goes into ``frozen_hours_at_detection`` and
    ``z_score`` stays ``None``, because a stuck hit has no z-score at all.
    This helper used to pass ``z_score=alert["duration_hours"]``, duplicating
    the pre-split production conversion — the one deliberately retained
    legacy-shape fixture now lives in
    ``tests/models/test_episode_duration_fallback.py``.
    """
    return UploadHighAnomalyDetail(
        score=1.0,
        timestamp=alert["timestamp"],
        detector=alert["detector"],
        column=alert["channel"],
        severity=alert["severity"],
        method=alert["method"],
        z_score=None,
        frozen_hours_at_detection=alert["duration_hours"],
        value=alert["value"],
    )


def _run_stuck_detection_on_df(
    normalized_df: pd.DataFrame,
) -> tuple[list[UploadHighAnomalyDetail], StuckCarry]:
    """Run stuck detection + flush on a single DataFrame (no LSTM)."""
    det = WallclockStuckDetector()
    carry = StuckCarry()
    raw_alerts = det.detect_chunk(normalized_df, carry)
    raw_alerts.extend(det.flush(carry))
    return [_dict_to_detail(a) for a in raw_alerts], carry


def _run_fixture_through_upload(fixture_path: Path) -> tuple[object, StuckCarry]:
    """Score a native fixture CSV through the full upload path, returning response + carry.

    Uses run_cold_run_upload with a mocked LSTM detector (scores don't matter
    for stuck-fault testing — we only inspect stuck_fault alerts).
    """
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

        # Capture the stuck_carry by patching finalize_scores
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
            # Use asyncio.run() with a fresh event loop to avoid interference
            # with pytest-asyncio's loop management in broader test runs.
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
                # Inside a running loop (pytest-asyncio) — use nest_asyncio or
                # run synchronously via loop.run_until_complete in a thread.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    response, _ = pool.submit(asyncio.run, coro).result()
            else:
                response, _ = asyncio.run(coro)

    carry = captured_carry[0] if captured_carry else StuckCarry()
    return response, carry


# ---------------------------------------------------------------------------
# Fixture A — stuck_fault detection
# ---------------------------------------------------------------------------

class TestFixtureAStuckFault:
    """Fixture A (native_dec_stuck_fault.csv) must produce stuck_fault/urgent alerts."""

    @pytest.fixture(autouse=True)
    def _load_fixture(self):
        assert FIXTURE_A.is_file(), f"Fixture A not found: {FIXTURE_A}"
        self.response, self.stuck_carry = _run_fixture_through_upload(FIXTURE_A)
        self.stuck_alerts = [
            h for h in self.response.high_anomalies
            if h.detector == "stuck_fault"
        ]

    def test_at_least_one_stuck_fault_alert_with_urgent_severity(self):
        """The upload path must detect at least one stuck_fault alert."""
        assert len(self.stuck_alerts) >= 1, (
            f"Expected ≥1 stuck_fault alert, got {len(self.stuck_alerts)}"
        )
        urgent_alerts = [a for a in self.stuck_alerts if a.severity == "urgent"]
        assert len(urgent_alerts) >= 1, (
            f"Expected ≥1 urgent alert, got severities: "
            f"{[a.severity for a in self.stuck_alerts]}"
        )

    def test_alert_names_frozen_channel_humidity(self):
        """The main fault alert must identify the humidity channel."""
        humidity_alerts = [
            a for a in self.stuck_alerts if a.column == "humidity"
        ]
        assert len(humidity_alerts) >= 1, (
            f"Expected ≥1 humidity stuck_fault, got columns: "
            f"{[a.column for a in self.stuck_alerts]}"
        )

    def test_alert_reports_duration_at_gate_crossing(self):
        """Every humidity alert reports elapsed-at-gate, not the full freeze.

        The gate-crossing duration now lives in
        ``frozen_hours_at_detection``; ``z_score`` is ``None`` for stuck
        hits. This test used to read ``[a.z_score for a in humidity_alerts
        if a.z_score is not None]`` and assert ``all(...)`` over it — after
        the split that comprehension is unconditionally empty and
        ``all([])`` is ``True``, so it reported green while asserting
        nothing. The count is pinned first so an empty or short list fails
        loudly instead of passing vacuously.
        """
        humidity_alerts = [
            a for a in self.stuck_alerts if a.column == "humidity"
        ]
        assert len(humidity_alerts) == EXPECTED_HUMIDITY_ALERT_COUNT, (
            f"Expected {EXPECTED_HUMIDITY_ALERT_COUNT} humidity stuck alerts, "
            f"got {len(humidity_alerts)}: "
            f"{[(a.timestamp, a.frozen_hours_at_detection) for a in humidity_alerts]}"
        )
        frozen_hours = [a.frozen_hours_at_detection for a in humidity_alerts]
        assert None not in frozen_hours, (
            f"Every stuck alert must carry frozen_hours_at_detection; got "
            f"{frozen_hours}"
        )
        assert all(
            abs(h - STUCK_WALLCLOCK_MIN_HOURS) < 1.0 for h in frozen_hours
        ), (
            f"Expected ≈ {STUCK_WALLCLOCK_MIN_HOURS} h frozen at the gate; "
            f"got {frozen_hours}"
        )

    def test_stuck_alert_carries_no_z_score(self):
        """``z_score`` is ``None`` on stuck hits — it no longer carries hours.

        Guards the overload from coming back: a numeric ``z_score`` on a
        stuck_fault row would be read as a z-score by every statistical
        display path, and by ``anomaly_rank_key``'s level-3 tiebreak.
        """
        humidity_alerts = [
            a for a in self.stuck_alerts if a.column == "humidity"
        ]
        assert len(humidity_alerts) == EXPECTED_HUMIDITY_ALERT_COUNT
        assert [a.z_score for a in humidity_alerts] == [None] * len(
            humidity_alerts
        )

    def test_main_fault_alert_onset_at_expected_timestamp(self):
        """The main stuck_fault alert must report onset 2026-12-18T13:00:00Z
        as its timestamp field and detected_ts strictly before freeze end."""
        # Identify the main fault by onset timestamp (≈ FAULT_ONSET).
        main_fault_alerts = [
            a for a in self.stuck_alerts
            if a.column == "humidity"
            and a.timestamp is not None
        ]
        # Find the alert whose onset is closest to the known fault onset.
        onset_match = [
            a for a in main_fault_alerts
            if abs(
                (datetime.fromisoformat(a.timestamp.replace("Z", "+00:00"))
                 - FAULT_ONSET).total_seconds()
            ) < 3600  # within 1 hour
        ]
        assert len(onset_match) >= 1, (
            f"No main fault alert near onset {FAULT_ONSET}; "
            f"got timestamps: {[a.timestamp for a in main_fault_alerts]}"
        )
        alert = onset_match[0]
        # Task 2b: detected_ts must be STRICTLY EARLIER than freeze end.
        freeze_end = datetime(2026, 12, 25, 8, 55, 0, tzinfo=timezone.utc)
        assert alert.detected_ts is not None, "detected_ts must be set"
        detected_dt = datetime.fromisoformat(
            alert.detected_ts.replace("Z", "+00:00")
        )
        assert detected_dt < freeze_end, (
            f"detected_ts {detected_dt} should be before freeze end {freeze_end}"
        )

    def test_cadence_not_flagged_for_5min_fixture(self):
        """Fixture A has uniform 300 s cadence — no flag expected."""
        assert not self.stuck_carry.is_cadence_flagged, (
            f"Cadence flagged unexpectedly; median={self.stuck_carry.cadence_median_seconds}"
        )


# ---------------------------------------------------------------------------
# Fixture B — no stuck_fault alerts
# ---------------------------------------------------------------------------

class TestFixtureBNoStuckFault:
    """Fixture B (native_july_clearsky.csv) must produce zero stuck_fault alerts."""

    @pytest.fixture(autouse=True)
    def _load_fixture(self):
        assert FIXTURE_B.is_file(), f"Fixture B not found: {FIXTURE_B}"
        self.response, self.stuck_carry = _run_fixture_through_upload(FIXTURE_B)
        self.stuck_alerts = [
            h for h in self.response.high_anomalies
            if h.detector == "stuck_fault"
        ]

    def test_zero_stuck_fault_alerts(self):
        """Clean July data must not trigger stuck-fault detection."""
        assert len(self.stuck_alerts) == 0, (
            f"Expected 0 stuck_fault alerts on clean fixture, got {len(self.stuck_alerts)}: "
            f"{[(a.column, a.frozen_hours_at_detection) for a in self.stuck_alerts]}"
        )


# ---------------------------------------------------------------------------
# Zero-variance window routing
# ---------------------------------------------------------------------------

class TestZeroVarianceRouting:
    """A synthetic zero-variance window must be caught by the stuck path,
    not silently dropped by the rolling_std >= 1e-9 guard."""

    def test_constant_humidity_detected_by_stuck_not_statistical(self):
        """Constant humidity (rolling_std=0) → stuck_fault, not stat drop."""
        # 200 rows at 5-min cadence = 16.67 hours > 6 h gate
        df = _build_constant_df(
            channel="humidity",
            value=100.0,
            n_rows=200,
            cadence_seconds=300,
        )

        # Run stuck detection
        alerts, _ = _run_stuck_detection_on_df(df)

        stuck_humidity = [
            a for a in alerts
            if a.detector == "stuck_fault" and a.column == "humidity"
        ]
        assert len(stuck_humidity) >= 1, (
            "Zero-variance humidity should be caught by stuck detector"
        )

        # Also verify the rolling z-score detector drops it
        from src.models.cold_run_detector import ColdRunStatisticalDetector

        detector = ColdRunStatisticalDetector()
        raw_rows = [
            {
                "timestamp": row.timestamp.isoformat(),
                "humidity": row.humidity,
                "temperature": row.temperature,
            }
            for row in df.itertuples()
        ]
        stat_hits = detector.detect_rolling(raw_rows, columns=["humidity"])
        humidity_stat_hits = [h for h in stat_hits if h.column == "humidity"]
        assert len(humidity_stat_hits) == 0, (
            "Constant humidity should be DROPPED by z-score detector "
            "(rolling_std < 1e-9 guard) — the stuck path is the only detector "
            "that catches it."
        )


# ---------------------------------------------------------------------------
# Cadence flag
# ---------------------------------------------------------------------------

class TestCadenceFlag:
    """A synthetic input at 15-minute cadence must trigger the cadence flag."""

    def test_15_minute_cadence_triggers_flag(self):
        """15-min cadence (900 s / 300 s = 3.0) exceeds tolerance factor."""
        df = _build_constant_df(
            channel="humidity",
            value=65.0,
            n_rows=100,
            cadence_seconds=900,  # 15 minutes
        )
        det = WallclockStuckDetector()
        carry = StuckCarry()
        det.detect_chunk(df, carry)
        det.flush(carry)

        assert carry.is_cadence_flagged, (
            f"Expected cadence flag for 900 s interval; "
            f"median={carry.cadence_median_seconds}"
        )
        assert carry.cadence_median_seconds is not None
        assert abs(carry.cadence_median_seconds - 900.0) < 1.0

    def test_5_minute_cadence_no_flag(self):
        """Standard 5-min cadence (300 s) should not trigger the flag."""
        df = _build_constant_df(
            channel="humidity",
            value=65.0,
            n_rows=100,
            cadence_seconds=300,
        )
        det = WallclockStuckDetector()
        carry = StuckCarry()
        det.detect_chunk(df, carry)
        det.flush(carry)

        assert not carry.is_cadence_flagged
        assert carry.cadence_median_seconds is not None
        assert abs(carry.cadence_median_seconds - 300.0) < 1.0


# ---------------------------------------------------------------------------
# Unit tests — gate logic
# ---------------------------------------------------------------------------

class TestStuckGateLogic:
    """Verify the wall-clock + sample-count gate works correctly."""

    def test_short_freeze_below_min_hours_not_alerted(self):
        """A freeze shorter than STUCK_WALLCLOCK_MIN_HOURS produces no alert."""
        hours_needed = STUCK_WALLCLOCK_MIN_HOURS
        # Create a freeze just under the threshold
        n_rows = int((hours_needed - 1) * 12)  # 5-min cadence
        df = _build_constant_df(
            channel="humidity", value=100.0, n_rows=max(n_rows, 10)
        )
        alerts, _ = _run_stuck_detection_on_df(df)
        humidity_alerts = [a for a in alerts if a.column == "humidity"]
        assert len(humidity_alerts) == 0

    def test_freeze_above_min_hours_alerted(self):
        """A freeze exceeding STUCK_WALLCLOCK_MIN_HOURS produces an alert."""
        hours_needed = STUCK_WALLCLOCK_MIN_HOURS
        n_rows = int((hours_needed + 2) * 12)  # 5-min cadence, +2 h margin
        df = _build_constant_df(
            channel="humidity", value=100.0, n_rows=n_rows
        )
        alerts, _ = _run_stuck_detection_on_df(df)
        humidity_alerts = [a for a in alerts if a.column == "humidity"]
        assert len(humidity_alerts) >= 1

    def test_nan_does_not_break_freeze(self):
        """NaN values inside a freeze must not break the frozen run."""
        n_rows = 200  # ~16.7 h at 5-min cadence
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        timestamps = [
            start + timedelta(seconds=i * 300) for i in range(n_rows)
        ]
        humidity = [100.0] * n_rows
        # Inject NaNs at several positions
        for idx in [10, 50, 100, 150]:
            humidity[idx] = float("nan")

        df = pd.DataFrame({
            "timestamp": timestamps,
            "temperature": [20.0] * n_rows,
            "humidity": humidity,
            "co2": [400.0] * n_rows,
            "light": [0.0] * n_rows,
        })
        alerts, _ = _run_stuck_detection_on_df(df)
        humidity_alerts = [a for a in alerts if a.column == "humidity"]
        assert len(humidity_alerts) >= 1, "NaN gaps should not break the freeze"

    def test_multi_chunk_freeze_detected(self):
        """A freeze spanning two score_portion calls must be detected."""
        chunk_size = 100
        total_rows = 250  # ~20.8 h at 5-min cadence
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        timestamps = [
            start + timedelta(seconds=i * 300) for i in range(total_rows)
        ]
        df = pd.DataFrame({
            "timestamp": timestamps,
            "temperature": [20.0 + 0.1 * (i % 50) for i in range(total_rows)],
            "humidity": [100.0] * total_rows,
            "co2": [400.0 + (i % 20) for i in range(total_rows)],
            "light": [0.0] * total_rows,
        })

        det = WallclockStuckDetector()
        carry = StuckCarry()
        raw_alerts: list[dict] = []

        # Process in two chunks
        chunk1 = df.iloc[:chunk_size].copy().reset_index(drop=True)
        chunk2 = df.iloc[chunk_size:].copy().reset_index(drop=True)

        raw_alerts.extend(det.detect_chunk(chunk1, carry))
        raw_alerts.extend(det.detect_chunk(chunk2, carry))
        raw_alerts.extend(det.flush(carry))

        all_alerts = [_dict_to_detail(a) for a in raw_alerts]
        humidity_alerts = [a for a in all_alerts if a.column == "humidity"]
        assert len(humidity_alerts) >= 1, (
            "Freeze spanning two chunks must be detected"
        )


# ---------------------------------------------------------------------------
# Measured latency reporter
# ---------------------------------------------------------------------------

def test_report_measured_latency():
    """Measure and assert detection latency for the main fault in fixture A.

    Task 2b: latency = detected_ts - onset, NOT onset - onset (which is
    tautologically zero).  The latency must be bounded by the configured
    gate + one sampling interval, and must be strictly less than the full
    fault duration (regression guard).
    """
    assert FIXTURE_A.is_file()
    response, _ = _run_fixture_through_upload(FIXTURE_A)
    stuck_alerts = [
        h for h in response.high_anomalies
        if h.detector == "stuck_fault" and h.column == "humidity"
    ]
    assert len(stuck_alerts) >= 1

    # Find the main fault alert by onset near FAULT_ONSET.
    main_alert = None
    for alert in stuck_alerts:
        ts = datetime.fromisoformat(alert.timestamp.replace("Z", "+00:00"))
        if abs((ts - FAULT_ONSET).total_seconds()) < 3600:
            main_alert = alert
            break
    assert main_alert is not None, "No alert near main fault onset"
    assert main_alert.detected_ts is not None, "detected_ts must be set"

    onset_dt = datetime.fromisoformat(
        main_alert.timestamp.replace("Z", "+00:00")
    )
    detected_dt = datetime.fromisoformat(
        main_alert.detected_ts.replace("Z", "+00:00")
    )
    latency_hours = (detected_dt - onset_dt).total_seconds() / 3600.0

    # Gate = STUCK_WALLCLOCK_MIN_HOURS; one sampling interval = 300 s = 0.083 h
    max_latency = STUCK_WALLCLOCK_MIN_HOURS + 0.1  # gate + margin
    assert latency_hours <= max_latency, (
        f"Latency {latency_hours:.2f} h exceeds gate {STUCK_WALLCLOCK_MIN_HOURS} h "
        f"+ margin; detected_ts={detected_dt}, onset={onset_dt}"
    )

    # Regression: latency must be strictly less than the full fault duration.
    assert latency_hours < FAULT_DURATION_HOURS, (
        f"Latency {latency_hours:.2f} h equals or exceeds full fault duration "
        f"{FAULT_DURATION_HOURS} h — detection is not early"
    )

    # Latency must NOT be zero (the tautological bug this test guards against).
    assert latency_hours > 0.0, (
        "Latency is exactly 0.00 h — this is the tautological bug "
        "(detected_ts == onset); investigate before reporting"
    )

    print(f"\n=== MEASURED DETECTION LATENCY ===")
    print(f"Real onset:       {onset_dt.isoformat()}")
    print(f"Detected at:      {detected_dt.isoformat()}")
    print(f"Latency:          {latency_hours:.2f} h")
    print(f"Gate threshold:   {STUCK_WALLCLOCK_MIN_HOURS} h")
    print(f"Reported elapsed: {main_alert.frozen_hours_at_detection} h")
    print(f"Frozen value:     {main_alert.value}")
    print(f"=================================\n")


# ---------------------------------------------------------------------------
# Routing pin — stuck rows never reach the statistical display path
# ---------------------------------------------------------------------------

class TestStuckRowsNeverReachStatJumpRows:
    """Stuck hits must never be fed to ``stat_rows_to_jump_rows``.

    That function applies the statistical ``min(1.0, z / 5.0)`` score formula
    to whatever it is handed. A stuck row has no z-score, so if the two paths
    ever merged, its real ``score=1.0`` would be replaced by the ``0.0``
    None-safe fallback and an urgent freeze would silently sink to the bottom
    of the display cap.

    Three independent mechanisms keep them apart today, and this class pins
    the composition of all three against fixture A's real pipeline output
    rather than just the outcome, so a future merge of the paths fails loudly:

    1. ``stat_rows_to_jump_rows`` is only ever called with the statistical
       list (``inference_results.py`` ``render_cold_run_anomaly_tables``).
    2. ``_statistical_anomalies_from_results`` prefers
       ``statistical_summary.anomalies``, whose rows are
       ``StatisticalAnomalyDetail`` dicts with no ``detector`` key at all.
    3. Its ``high_anomalies`` fallback admits only rows whose detector is
       statistical (plain or compound) *and* which have a truthy ``column``.

    This lives here, next to the harness that produces a real fixture A
    response, rather than in ``tests/dashboard/`` — the pure-mechanism pins
    for items 2 and 3 above are in
    ``tests/dashboard/test_stuck_row_routing.py``.
    """

    @pytest.fixture(autouse=True)
    def _load_fixture(self):
        assert FIXTURE_A.is_file(), f"Fixture A not found: {FIXTURE_A}"
        response, _ = _run_fixture_through_upload(FIXTURE_A)
        self.results = response.model_dump(mode="json")

    @staticmethod
    def _is_stuck(row: dict) -> bool:
        """Detector membership, never exact string equality.

        ``"lstm+statistical"`` is a real compound label, so exact-match
        filtering on detector has undercounted here before.
        """
        return "stuck_fault" in str(row.get("detector") or "").split("+")

    def test_fixture_a_actually_contains_stuck_rows(self):
        """Non-vacuity guard for the two pins below.

        If fixture A ever stopped producing stuck rows, the "no stuck rows
        in the statistical list" assertions would pass for the wrong reason.
        """
        stuck = [r for r in self.results["high_anomalies"] if self._is_stuck(r)]
        assert len(stuck) == EXPECTED_HUMIDITY_ALERT_COUNT, (
            f"Expected {EXPECTED_HUMIDITY_ALERT_COUNT} stuck rows in "
            f"high_anomalies, got {len(stuck)}"
        )

    def test_statistical_list_admits_no_stuck_rows(self):
        """The list handed to ``stat_rows_to_jump_rows`` holds no stuck rows."""
        from src.dashboard.components.inference_results import (
            scoped_upload_anomalies,
        )

        stat_rows, high_rows, used_fallback = scoped_upload_anomalies(
            self.results, None, None
        )
        assert not used_fallback
        assert stat_rows, "Statistical list is empty — pin would be vacuous"
        assert [r for r in stat_rows if self._is_stuck(r)] == []
        # The stuck rows do survive the *merged* list's cap, so their absence
        # from stat_rows is routing, not the display cap dropping them.
        assert len([r for r in high_rows if self._is_stuck(r)]) == (
            EXPECTED_HUMIDITY_ALERT_COUNT
        )

    def test_every_row_reaching_jump_rows_has_a_usable_z_score(self):
        """The precondition of the ``min(1.0, z / 5.0)`` score formula holds.

        Asserting on the resulting score is not enough to catch a path
        merge: a real stuck row already carries ``score=1.0``, and
        ``stat_rows_to_jump_rows`` uses ``setdefault``, so the leaked row
        would keep its score and the 0.0 fallback would never show.
        Verified by simulation — a merge that leaks stuck rows here is
        caught by ``test_statistical_list_admits_no_stuck_rows`` and by this
        test's ``z_score is not None``, but not by a score assertion.
        """
        from src.dashboard.components.inference_results import (
            scoped_upload_anomalies,
            stat_rows_to_jump_rows,
        )

        stat_rows, _, _ = scoped_upload_anomalies(self.results, None, None)
        jump_rows = stat_rows_to_jump_rows(stat_rows)
        assert len(jump_rows) == len(stat_rows)
        assert jump_rows, "Jump rows are empty — pin would be vacuous"
        unusable = [r for r in jump_rows if not isinstance(r.get("z_score"), float)]
        assert unusable == [], (
            "A row reached stat_rows_to_jump_rows with no numeric z_score, so "
            "the statistical score formula cannot apply to it — most likely a "
            f"stuck row leaked into the statistical path: {unusable[:3]}"
        )
        assert [r for r in jump_rows if r["score"] == 0.0] == []

"""Tests for query-time anomaly episode merging (/api/anomalies/episodes).

The pure-grouper tests need no database — ``build_episodes`` is I/O-free.  The
endpoint tests cover the one constraint that must not be violated: episodes are
derived from every flagged row in the window, never from a top-N-by-score
subset (see ``anomaly_episode_service`` module docstring).
"""

import datetime

import pytest
import pytest_asyncio
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.main import app
from src.api.services.anomaly_episode_grouping import build_episodes
from src.db.models import Base, SensorReading
from src.db.session import get_db

TENANT = "tenant_episodes"
BASE_TS = datetime.datetime(2026, 12, 25, 9, 0, 0, tzinfo=datetime.timezone.utc)
SAMPLE_INTERVAL = datetime.timedelta(minutes=5)

# Humidity band lower bound is 50% (HUMIDITY_RANGE) — values below it read as
# a "drop"; values above the 80% upper bound read as a "spike".
HUMIDITY_DROP = 30.0
HUMIDITY_SPIKE = 99.0


def _row(
    minutes: int,
    *,
    value: float = HUMIDITY_DROP,
    score: float = 0.8,
    channel: str | None = "humidity",
    detector: str | None = "statistical",
    severity: str = "warning",
) -> dict:
    """One flagged row dict in the shape ``_row_dict_from_minimal`` produces."""
    ts = BASE_TS + datetime.timedelta(minutes=minutes)
    return {
        "timestamp": ts.isoformat(),
        "anomaly_score": score,
        "temperature": 22.0,
        "humidity": value if channel == "humidity" else 60.0,
        "co2": 400.0,
        "light": 500.0,
        "anomaly_type": None,
        "trigger_channel": channel,
        "trigger_detector": detector,
        "detected_severity": severity,
        "trigger_value": value,
    }


class TestContiguity:
    def test_consecutive_samples_merge_into_one_episode(self):
        episodes = build_episodes([_row(m) for m in (0, 5, 10, 15)])
        assert len(episodes) == 1
        assert episodes[0]["sample_count"] == 4
        assert episodes[0]["duration_seconds"] == 900.0

    def test_gap_beyond_threshold_splits_the_episode(self):
        # 0,5 then a 30-minute gap then 35,40 — the gap exceeds the 600 s rule.
        episodes = build_episodes([_row(m) for m in (0, 5, 35, 40)])
        assert len(episodes) == 2
        assert [e["sample_count"] for e in episodes] == [2, 2]

    def test_gap_exactly_at_threshold_still_merges(self):
        # 600 s gap is "within" the rule — the split is on gap > gap_seconds.
        episodes = build_episodes([_row(0), _row(10)])
        assert len(episodes) == 1

    def test_single_sample_episode_has_zero_duration_and_no_slope(self):
        episodes = build_episodes([_row(0)])
        assert len(episodes) == 1
        assert episodes[0]["duration_seconds"] == 0.0
        assert episodes[0]["trend"] == "flat"
        assert episodes[0]["slope_per_hour"] is None


class TestGroupingKey:
    def test_opposite_directions_never_merge(self):
        """A humidity drop and a humidity spike minutes apart are not one event."""
        rows = [
            _row(0, value=HUMIDITY_DROP),
            _row(5, value=HUMIDITY_DROP),
            _row(10, value=HUMIDITY_SPIKE),
            _row(15, value=HUMIDITY_SPIKE),
        ]
        episodes = build_episodes(rows)
        assert len(episodes) == 2
        assert {e["direction"] for e in episodes} == {"drop", "spike"}

    def test_different_detectors_never_merge(self):
        """A stuck_fault must never be absorbed into a statistical episode."""
        rows = [
            _row(0, detector="statistical"),
            _row(5, detector="stuck_fault"),
        ]
        episodes = build_episodes(rows)
        assert len(episodes) == 2
        assert {e["detector"] for e in episodes} == {"statistical", "stuck_fault"}

    def test_different_channels_never_merge(self):
        rows = [_row(0, channel="humidity"), _row(5, channel="temperature", value=15.0)]
        episodes = build_episodes(rows)
        assert len(episodes) == 2
        assert {e["channel"] for e in episodes} == {"humidity", "temperature"}

    def test_null_trigger_channel_falls_back_to_value_inference(self):
        """Rows pre-dating migration 024 have no trigger_channel/detector."""
        rows = [_row(m, channel=None, detector=None) for m in (0, 5)]
        for row in rows:
            row["humidity"] = HUMIDITY_DROP
            row["trigger_value"] = None
        episodes = build_episodes(rows)
        assert len(episodes) == 1
        assert episodes[0]["channel"] == "humidity"
        assert episodes[0]["detector"] == "unknown"


class TestTrendAndExtremum:
    def test_deepening_drop_reads_as_worsening(self):
        rows = [_row(m, value=v) for m, v in zip((0, 5, 10, 15), (45.0, 40.0, 35.0, 30.0))]
        assert build_episodes(rows)[0]["trend"] == "worsening"

    def test_recovering_drop_reads_as_easing(self):
        rows = [_row(m, value=v) for m, v in zip((0, 5, 10, 15), (30.0, 35.0, 40.0, 45.0))]
        assert build_episodes(rows)[0]["trend"] == "easing"

    def test_noise_below_the_deadband_reads_as_flat(self):
        rows = [_row(m, value=v) for m, v in zip((0, 5, 10, 15), (30.0, 30.1, 30.0, 30.1))]
        assert build_episodes(rows)[0]["trend"] == "flat"

    def test_peak_value_is_the_extremum_not_the_highest_scoring_row(self):
        """Regression: the headline must quote the worst reading reached.

        The highest-scoring row here carries 32.0, but humidity bottomed at
        29.0.  Reporting 32.0 tells the operator the drop was milder than it
        was.
        """
        rows = [
            _row(0, value=32.0, score=0.95),   # highest score, mildest value
            _row(5, value=29.0, score=0.70),   # true extremum
            _row(10, value=30.0, score=0.80),
        ]
        episode = build_episodes(rows)[0]
        assert episode["peak_value"] == 29.0
        assert "29.0" in episode["headline"]
        assert "32.0" not in episode["headline"]
        # peak_score / peak_ts still describe the highest-scoring row.
        assert episode["peak_score"] == pytest.approx(0.95)
        assert episode["peak_ts"].startswith("2026-12-25T09:00")

    def test_spike_extremum_is_the_maximum(self):
        rows = [_row(m, value=v) for m, v in zip((0, 5, 10), (85.0, 99.0, 90.0))]
        assert build_episodes(rows)[0]["peak_value"] == 99.0

    def test_non_finite_trigger_value_is_guarded(self):
        """Postgres 500s on NaN in a JSON column where SQLite accepts it."""
        row = _row(0)
        row["trigger_value"] = float("nan")
        row["humidity"] = float("nan")
        episode = build_episodes([row])[0]
        assert episode["peak_value"] is None
        assert episode["first_value"] is None


class TestTruncationFlags:
    """The flags mark episodes clipped by the QUERIED window, not by the data."""

    def test_no_window_bounds_means_nothing_is_truncated(self):
        episodes = build_episodes([_row(m) for m in (0, 5, 10)])
        assert episodes[0]["is_truncated_start"] is False
        assert episodes[0]["is_truncated_end"] is False

    def test_wide_window_leaves_flags_clear(self):
        """Regression: flags must not fire just because an episode is first/last.

        Deriving the bounds from min/max of the returned rows marked the first
        and last episode of every query as truncated, always.
        """
        episodes = build_episodes(
            [_row(m) for m in (0, 5, 10)],
            window_start=BASE_TS - datetime.timedelta(days=1),
            window_end=BASE_TS + datetime.timedelta(days=1),
        )
        assert episodes[0]["is_truncated_start"] is False
        assert episodes[0]["is_truncated_end"] is False

    def test_window_cutting_an_episode_flags_both_edges(self):
        rows = [_row(m) for m in (0, 5, 10)]
        episodes = build_episodes(
            rows, window_start=BASE_TS, window_end=BASE_TS + datetime.timedelta(minutes=10),
        )
        assert episodes[0]["is_truncated_start"] is True
        assert episodes[0]["is_truncated_end"] is True


# ─── endpoint tests ──────────────────────────────────────────────────────────

# 12 contiguous 5-minute humidity-drop rows.  Scores are shaped so that a
# top-N-by-score cut would drop the middle of the run and fracture it: the
# first and last thirds score high, the middle third scores just above the
# threshold.
EPISODE_MINUTES = list(range(0, 60, 5))
LOW_SCORE_MINUTES = {20, 25, 30}


@pytest_asyncio.fixture
async def episode_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        for minutes in EPISODE_MINUTES:
            score = 0.55 if minutes in LOW_SCORE_MINUTES else 0.90
            session.add(
                SensorReading(
                    tenant_id=TENANT,
                    greenhouse_id="default",
                    timestamp=BASE_TS + datetime.timedelta(minutes=minutes),
                    temperature=22.0,
                    humidity=HUMIDITY_DROP,
                    co2=400.0,
                    light=500.0,
                    anomaly_score=score,
                    trigger_channel="humidity",
                    trigger_detector="statistical",
                    detected_severity="warning",
                    trigger_value=HUMIDITY_DROP,
                )
            )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def episode_client(episode_database):
    async def override_get_db():
        yield episode_database

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


EPISODE_WINDOW = {
    "tenant_id": TENANT,
    "start": (BASE_TS - datetime.timedelta(hours=1)).isoformat(),
    "end": (BASE_TS + datetime.timedelta(hours=2)).isoformat(),
    "threshold": 0.5,
}


class TestEpisodesEndpoint:
    def test_contiguous_run_returns_one_episode(self, episode_client):
        response = episode_client.get("/api/anomalies/episodes", params=EPISODE_WINDOW)
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["episode_count"] == 1
        episode = payload["episodes"][0]
        assert episode["sample_count"] == len(EPISODE_MINUTES)
        assert episode["duration_seconds"] == 55 * 60
        assert episode["channel"] == "humidity"
        assert episode["direction"] == "drop"

    def test_episodes_are_built_from_all_rows_not_a_top_n_subset(self, episode_client):
        """The constraint that must not be violated.

        The middle of the run scores below the top rows.  If episodes were
        derived from a top-N-by-score list, those samples would be missing and
        the single run would fracture into two episodes around the hole.
        """
        response = episode_client.get("/api/anomalies/episodes", params=EPISODE_WINDOW)
        payload = response.json()
        assert payload["episode_count"] == 1, (
            "run fractured — episodes were built from a score-ranked subset"
        )
        assert payload["episodes"][0]["sample_count"] == len(EPISODE_MINUTES)

    def test_anomaly_count_reports_every_flagged_row(self, episode_client):
        payload = episode_client.get(
            "/api/anomalies/episodes", params=EPISODE_WINDOW
        ).json()
        assert payload["anomaly_count"] == len(EPISODE_MINUTES)

    def test_threshold_above_the_dip_fractures_the_run(self, episode_client):
        """Raising the threshold past the middle scores is a real split."""
        payload = episode_client.get(
            "/api/anomalies/episodes", params={**EPISODE_WINDOW, "threshold": 0.8}
        ).json()
        assert payload["episode_count"] == 2

    def test_samples_are_capped_and_enriched(self, episode_client):
        episode = episode_client.get(
            "/api/anomalies/episodes", params=EPISODE_WINDOW
        ).json()["episodes"][0]
        assert 0 < len(episode["samples"]) <= episode["sample_count"]
        assert episode["samples"][0]["timestamp"]

    def test_empty_window_returns_no_episodes(self, episode_client):
        payload = episode_client.get(
            "/api/anomalies/episodes",
            params={
                **EPISODE_WINDOW,
                "start": (BASE_TS + datetime.timedelta(days=5)).isoformat(),
                "end": (BASE_TS + datetime.timedelta(days=6)).isoformat(),
            },
        ).json()
        assert payload["episodes"] == []
        assert payload["episode_count"] == 0

    def test_threshold_defaults_to_the_canonical_value(self, episode_client):
        params = {k: v for k, v in EPISODE_WINDOW.items() if k != "threshold"}
        payload = episode_client.get("/api/anomalies/episodes", params=params).json()
        assert payload["threshold"] == 0.5
        assert payload["score_scale_version"] == 2

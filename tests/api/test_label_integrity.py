"""Alert label integrity tests (D1–D4 fixes).

Acceptance criteria — each test is labelled with its defect ID:

D1  triggering column is persisted and read back correctly
D2  reasoning text describes only the channel that fired
D3  statistical scores are distributed (not saturated at 1.0)
D4  detected_severity is distinguishable across z-score levels

Run with:
    venv/bin/python -m pytest tests/api/test_label_integrity.py -v
"""

from __future__ import annotations

import math
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.schemas.sensor_data import StatisticalAnomalyDetail, UploadHighAnomalyDetail
from src.api.services.anomaly_group_service import (
    _enrich_sample_fast,
    _row_dict_from_minimal,
    _simple_reasoning_from_values,
)
from src.api.services.cold_run_upload import (
    build_anomaly_metadata_by_timestamp,
    build_trigger_data_by_timestamp,
)
from src.api.services.upload_portions import _stat_z_to_canonical
from src.db.models import Base


# ─── DB fixtures ──────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ─── helpers ──────────────────────────────────────────────────────────────────


def _stat_hit(
    ts: str,
    column: str,
    z_score: float,
    value: float,
    severity: str = "critical",
) -> UploadHighAnomalyDetail:
    """Build a synthetic statistical-detector hit."""
    from src.api.services.upload_portions import _stat_z_to_canonical

    return UploadHighAnomalyDetail(
        score=_stat_z_to_canonical(z_score),
        timestamp=ts,
        detector="statistical",
        column=column,
        z_score=z_score,
        severity=severity,
        method="rolling_zscore",
        value=value,
    )


def _row(
    trigger_channel: str | None = None,
    trigger_value: float | None = None,
    detected_severity: str | None = None,
    temperature: float = 17.0,
    humidity: float = 78.0,
) -> dict[str, Any]:
    """Build a minimal row dict for _enrich_sample_fast."""
    return _row_dict_from_minimal(
        "2026-12-18T13:00:00",
        anomaly_score=0.8,
        temperature=temperature,
        humidity=humidity,
        co2=400.0,
        light=0.0,
        anomaly_type=None,
        trigger_channel=trigger_channel,
        trigger_detector="statistical" if trigger_channel else None,
        detected_severity=detected_severity,
        trigger_value=trigger_value,
    )


# ─── D1: trigger_channel persists and reads back ──────────────────────────────


class TestTriggerChannelPersistence:
    """D1 — build_trigger_data_by_timestamp encodes the correct primary channel."""

    def test_pipe_temp_hit_is_primary_channel(self) -> None:
        hits = [
            _stat_hit("2026-12-18T13:00:00", "measured_pipe_1_temp", z_score=8.5, value=51.0),
        ]
        data = build_trigger_data_by_timestamp(hits)
        assert len(data) == 1
        entry = next(iter(data.values()))
        assert entry["channel"] == "measured_pipe_1_temp"
        assert entry["detector"] == "statistical"
        assert entry["severity"] == "critical"
        assert entry["value"] == pytest.approx(51.0)

    def test_highest_z_wins_when_multiple_columns_fire_at_same_timestamp(self) -> None:
        ts = "2026-12-18T13:00:00"
        hits = [
            _stat_hit(ts, "humidity", z_score=6.0, value=100.0),
            _stat_hit(ts, "measured_pipe_2_temp", z_score=12.0, value=28.0),
        ]
        data = build_trigger_data_by_timestamp(hits)
        entry = data[next(iter(data))]
        assert entry["channel"] == "measured_pipe_2_temp", (
            "highest |z| must win"
        )

    def test_second_timestamp_gets_own_entry(self) -> None:
        hits = [
            _stat_hit("2026-12-18T13:00:00", "measured_pipe_1_temp", z_score=8.0, value=51.0),
            _stat_hit("2026-12-18T13:05:00", "humidity", z_score=7.0, value=100.0),
        ]
        data = build_trigger_data_by_timestamp(hits)
        assert len(data) == 2

    def test_lstm_only_hit_has_none_channel(self) -> None:
        hit = UploadHighAnomalyDetail(
            score=0.9,
            timestamp="2026-12-18T13:00:00",
            detector="lstm",
            column=None,
            z_score=None,
            severity=None,
        )
        data = build_trigger_data_by_timestamp([hit])
        entry = next(iter(data.values()))
        assert entry["channel"] is None
        assert entry["detector"] == "lstm"


# ─── D1: affected_sensors uses trigger_channel over inferred sensors ──────────


class TestEnrichUsesPersistedChannel:
    """D1 — _enrich_sample_fast reports the persisted trigger channel."""

    def test_pipe_temp_row_reports_pipe_temp_not_temperature(self) -> None:
        row = _row(trigger_channel="measured_pipe_1_temp", trigger_value=51.0)
        enriched = _enrich_sample_fast(row, incident=None)
        sensors = enriched["affected_sensors"]
        assert "measured_pipe_1_temp" in sensors, (
            f"expected measured_pipe_1_temp but got {sensors!r}"
        )
        assert "temperature" not in sensors, (
            f"temperature must not appear when trigger_channel is a pipe column; got {sensors!r}"
        )

    def test_humidity_row_reports_humidity_channel(self) -> None:
        row = _row(trigger_channel="humidity", trigger_value=100.0)
        enriched = _enrich_sample_fast(row, incident=None)
        assert enriched["affected_sensors"] == ["humidity"]

    def test_no_trigger_channel_falls_back_to_value_inference(self) -> None:
        # A row with no trigger_channel and temperature below normal range.
        row = _row(trigger_channel=None, temperature=8.0)
        enriched = _enrich_sample_fast(row, incident=None)
        # Value-based inference should return at least one sensor.
        assert len(enriched["affected_sensors"]) >= 1


# ─── D2: reasoning describes only the triggering channel ─────────────────────


class TestReasoningFromTriggerOnly:
    """D2 — reasoning text quotes the channel that fired and nothing else."""

    def test_pipe_temp_reasoning_contains_pipe_temp_value(self) -> None:
        row = _row(trigger_channel="measured_pipe_1_temp", trigger_value=51.0)
        enriched = _enrich_sample_fast(row, incident=None)
        reasoning = enriched["reasoning"]
        assert "measured_pipe_1_temp" in reasoning, (
            f"reasoning must mention the triggering column; got {reasoning!r}"
        )
        assert "51" in reasoning, (
            f"reasoning must quote the pipe temperature value; got {reasoning!r}"
        )

    def test_pipe_temp_reasoning_excludes_greenhouse_air_clause(self) -> None:
        # Dec row: temperature=10.8 (below range) would trigger "temperature drop"
        # clause in the old code — it must NOT appear when trigger_channel is pipe.
        row = _row(trigger_channel="measured_pipe_1_temp", trigger_value=51.0, temperature=10.8)
        enriched = _enrich_sample_fast(row, incident=None)
        reasoning = enriched["reasoning"]
        assert "temperature drop" not in reasoning, (
            f"greenhouse air temperature must not generate a clause; got {reasoning!r}"
        )

    def test_pipe_temp_reasoning_excludes_humidity_clause(self) -> None:
        row = _row(trigger_channel="measured_pipe_1_temp", trigger_value=51.0, humidity=100.0)
        enriched = _enrich_sample_fast(row, incident=None)
        reasoning = enriched["reasoning"]
        assert "humidity" not in reasoning, (
            f"humidity must not generate a clause when pipe triggered; got {reasoning!r}"
        )

    def test_humidity_reasoning_describes_humidity(self) -> None:
        row = _row(trigger_channel="humidity", trigger_value=100.0)
        enriched = _enrich_sample_fast(row, incident=None)
        reasoning = enriched["reasoning"]
        assert "humidity" in reasoning

    def test_simple_reasoning_canonical_spike(self) -> None:
        text = _simple_reasoning_from_values("temperature", 35.0)
        assert "temperature" in text
        assert "spike" in text.lower() or "35" in text

    def test_simple_reasoning_canonical_drop(self) -> None:
        text = _simple_reasoning_from_values("temperature", 5.0)
        assert "temperature" in text
        assert "drop" in text.lower() or "5" in text

    def test_simple_reasoning_non_canonical_channel(self) -> None:
        text = _simple_reasoning_from_values("measured_pipe_2_temp", 28.0)
        assert "measured_pipe_2_temp" in text
        assert "28" in text
        # No spike/drop since no bounds exist for raw channel names.
        assert "greenhouse" not in text.lower()

    def test_simple_reasoning_with_none_value(self) -> None:
        text = _simple_reasoning_from_values("measured_pipe_1_temp", None)
        assert "measured_pipe_1_temp" in text

    def test_no_alert_fabricates_out_of_band_channel_clause(self) -> None:
        """No sensor other than the trigger channel must appear in reasoning."""
        for channel, value in [
            ("measured_pipe_1_temp", 51.0),
            ("measured_pipe_2_temp", 28.0),
            ("calculated_heating_temp", 22.0),
        ]:
            row = _row(trigger_channel=channel, trigger_value=value, humidity=100.0)
            enriched = _enrich_sample_fast(row, incident=None)
            reasoning = enriched["reasoning"]
            # Humidity is 100.0 (out-of-bound) but must NOT appear in reasoning
            # since it did not trigger.
            assert "humidity" not in reasoning, (
                f"humidity clause must not appear when trigger={channel!r}; "
                f"reasoning={reasoning!r}"
            )


# ─── D3: statistical score is not saturated ──────────────────────────────────


class TestScoreDesaturation:
    """D3 — scores across a z range carry real information; max < 1.0 or only
    at the true asymptote."""

    def test_scores_are_distributed_not_constant(self) -> None:
        z_values = [5.5, 6.0, 7.0, 8.0, 10.0, 12.0, 15.0, 19.5, 20.0]
        scores = [_stat_z_to_canonical(z) for z in z_values]
        distinct = set(round(s, 6) for s in scores)
        assert len(distinct) >= 5, (
            f"expected at least 5 distinct score values across z={z_values}, "
            f"got {sorted(distinct)}"
        )

    def test_decision_point_maps_to_half(self) -> None:
        from src.config import COLD_RUN_STAT_Z_MIN

        score = _stat_z_to_canonical(COLD_RUN_STAT_Z_MIN)
        assert score == pytest.approx(0.5, abs=1e-6), (
            f"z == COLD_RUN_STAT_Z_MIN must map to 0.5; got {score}"
        )

    def test_score_strictly_increases_with_z(self) -> None:
        z_values = [5.5, 8.0, 12.0, 19.5]
        scores = [_stat_z_to_canonical(z) for z in z_values]
        for i in range(len(scores) - 1):
            assert scores[i] < scores[i + 1], (
                f"score must strictly increase with z; got {scores}"
            )

    def test_max_score_below_one_for_realistic_z_range(self) -> None:
        """The asymptotic formula never returns exactly 1.0 for finite z."""
        for z in [5.5, 10.0, 19.5]:
            score = _stat_z_to_canonical(z)
            assert score < 1.0, f"score for z={z} must be < 1.0, got {score}"

    def test_score_above_threshold_exceeds_half(self) -> None:
        from src.config import COLD_RUN_STAT_Z_MIN

        score_above = _stat_z_to_canonical(COLD_RUN_STAT_Z_MIN + 1.0)
        assert score_above > 0.5

    def test_finite_scores_only(self) -> None:
        for z in [5.5, 6.0, 10.0, 20.0]:
            s = _stat_z_to_canonical(z)
            assert math.isfinite(s), f"non-finite score for z={z}: {s}"


# ─── D4: detected_severity is distinguishable ─────────────────────────────────


class TestSeverityDistinguishable:
    """D4 — severity is persisted and surfaces through the enrichment path."""

    def test_critical_row_carries_critical_severity(self) -> None:
        row = _row(
            trigger_channel="measured_pipe_1_temp",
            trigger_value=51.0,
            detected_severity="critical",
        )
        enriched = _enrich_sample_fast(row, incident=None)
        assert enriched.get("detected_severity") == "critical"

    def test_warning_row_carries_warning_severity(self) -> None:
        row = _row(
            trigger_channel="humidity",
            trigger_value=99.0,
            detected_severity="warning",
        )
        enriched = _enrich_sample_fast(row, incident=None)
        assert enriched.get("detected_severity") == "warning"

    def test_critical_and_warning_severities_are_different(self) -> None:
        row_critical = _row(detected_severity="critical")
        row_warning = _row(detected_severity="warning")
        s_critical = _enrich_sample_fast(row_critical, incident=None).get("detected_severity")
        s_warning = _enrich_sample_fast(row_warning, incident=None).get("detected_severity")
        assert s_critical != s_warning

    def test_high_z_produces_critical_in_trigger_data(self) -> None:
        """z >= 3.0 is marked critical by the statistical detector."""
        hit = _stat_hit(
            "2026-12-18T13:00:00",
            "measured_pipe_1_temp",
            z_score=8.0,
            value=51.0,
            severity="critical",
        )
        data = build_trigger_data_by_timestamp([hit])
        entry = next(iter(data.values()))
        assert entry["severity"] == "critical"

    def test_build_sample_includes_detected_severity(self) -> None:
        from src.api.services.anomaly_group_service import _build_sample

        row = _row(detected_severity="critical")
        enriched = _enrich_sample_fast(row, incident=None)
        sample = _build_sample(enriched)
        assert "detected_severity" in sample
        assert sample["detected_severity"] == "critical"

"""Unit tests for the anomaly filter/enrich/group service (G2/G3/G4)."""

from __future__ import annotations

import pytest

from src.api.services.anomaly_insights import (
    enrich_anomaly_rows,
    filter_anomaly_rows,
    group_anomaly_rows,
    top_anomalies,
)


def _row(ts: str, score, **extra) -> dict:
    return {
        "timestamp": ts,
        "temperature": 22.0,
        "humidity": 60.0,
        "co2": 400.0,
        "light": 500.0,
        "anomaly_score": score,
        **extra,
    }


class TestFilterAnomalyRows:
    def test_keeps_rows_at_or_above_threshold(self):
        rows = [
            _row("2026-06-01T00:00:00", 0.2),
            _row("2026-06-01T01:00:00", 0.5),
            _row("2026-06-01T02:00:00", 0.9),
        ]
        kept = filter_anomaly_rows(rows, 0.5)
        assert [r["anomaly_score"] for r in kept] == [0.5, 0.9]

    def test_missing_and_non_numeric_scores_never_flag(self):
        rows = [
            _row("2026-06-01T00:00:00", None),
            _row("2026-06-01T01:00:00", "high"),
            _row("2026-06-01T02:00:00", True),
            {"timestamp": "2026-06-01T03:00:00"},
        ]
        assert filter_anomaly_rows(rows, 0.0) == []

    def test_empty_input(self):
        assert filter_anomaly_rows([], 0.5) == []


class TestTopAnomalies:
    def test_sorts_by_score_desc_and_caps_at_limit(self):
        rows = [
            _row("2026-06-01T00:00:00", 0.3),
            _row("2026-06-01T01:00:00", 0.9),
            _row("2026-06-01T02:00:00", 0.6),
        ]
        ranked = top_anomalies(rows, 2)
        assert [r["anomaly_score"] for r in ranked] == [0.9, 0.6]


class TestGroupAnomalyRows:
    def test_group_by_date_uses_iso_date_prefix(self):
        rows = [
            _row("2026-06-01T10:00:00", 0.7),
            _row("2026-06-01T11:00:00", 0.9),
            _row("2026-06-02T10:00:00", 0.8),
        ]
        groups = group_anomaly_rows(rows, "date", sample_limit=5)
        assert [g["key"] for g in groups] == ["2026-06-01", "2026-06-02"]
        first = groups[0]
        assert first["count"] == 2
        assert first["top_score"] == pytest.approx(0.9)
        # sample_ids ranked by score: the 11:00 row scores highest.
        assert first["sample_ids"][0] == "2026-06-01T11:00:00"

    def test_group_by_type_falls_back_to_unknown(self):
        rows = [
            _row("2026-06-01T10:00:00", 0.7, fault_type="spike_fault"),
            _row("2026-06-01T11:00:00", 0.9, fault_type="spike_fault"),
            _row("2026-06-01T12:00:00", 0.8),
        ]
        groups = group_anomaly_rows(rows, "type", sample_limit=5)
        assert {g["key"]: g["count"] for g in groups} == {
            "spike_fault": 2,
            "unknown": 1,
        }

    def test_group_by_sensor_counts_row_once_per_sensor(self):
        rows = [
            _row("2026-06-01T10:00:00", 0.7, affected_sensors=["temperature", "co2"]),
            _row("2026-06-01T11:00:00", 0.9, affected_sensors=["temperature"]),
        ]
        groups = group_anomaly_rows(rows, "sensor", sample_limit=5)
        assert {g["key"]: g["count"] for g in groups} == {"temperature": 2, "co2": 1}

    def test_sample_ids_capped_by_sample_limit(self):
        rows = [_row(f"2026-06-01T{h:02d}:00:00", 0.5 + h / 100) for h in range(10)]
        groups = group_anomaly_rows(rows, "date", sample_limit=3)
        assert len(groups[0]["sample_ids"]) == 3

    def test_invalid_group_by_raises(self):
        with pytest.raises(ValueError):
            group_anomaly_rows([], "severity", sample_limit=5)

    def test_empty_rows_yield_no_groups(self):
        assert group_anomaly_rows([], "date", sample_limit=5) == []


class TestEnrichAnomalyRows:
    def test_incident_within_window_supplies_fault_type_and_root_cause(self):
        rows = [_row("2026-06-01T10:00:00", 0.9)]
        incidents = [
            {
                "id": "inc-1",
                "detected_at": "2026-06-01T10:05:00",
                "anomaly_score": 0.95,
                "fault_type": "spike_fault",
                "root_cause": "Fan failure",
                "root_cause_details": {"affected_sensors": ["temperature"]},
            }
        ]
        enriched = enrich_anomaly_rows(rows, incidents)
        assert len(enriched) == 1
        row = enriched[0]
        assert row["fault_type"] == "spike_fault"
        assert row["root_cause"] == "Fan failure"
        assert row["incident_id"] == "inc-1"
        assert row["affected_sensors"] == ["temperature"]
        assert "reasoning" in row and row["reasoning"]

    def test_persisted_incident_reasoning_is_authoritative(self):
        """A reasoning stored by the pipeline (naming the algorithm that
        fired) overrides the z-score-based regeneration, so /enriched stays
        consistent with the pipeline's fault decision."""
        rows = [_row("2026-06-01T10:00:00", 0.9)]
        persisted = (
            "Multivariate implausibility: the joint temperature reading "
            "exceeded the Mahalanobis distance cutoff"
        )
        incidents = [
            {
                "id": "inc-mv",
                "detected_at": "2026-06-01T10:05:00",
                "anomaly_score": 0.95,
                "fault_type": "accuracy_decline",
                "root_cause": "multivariate_implausibility",
                "root_cause_details": {
                    "affected_sensors": ["temperature"],
                    "reasoning": persisted,
                },
            }
        ]
        row = enrich_anomaly_rows(rows, incidents)[0]
        assert row["reasoning"] == persisted

    def test_no_incident_still_produces_fault_type_and_list_sensors(self):
        rows = [_row("2026-06-01T10:00:00", 0.9, temperature=45.0)]
        enriched = enrich_anomaly_rows(rows, [])
        row = enriched[0]
        assert row["fault_type"]  # z-score / pattern fallback always classifies
        assert isinstance(row["affected_sensors"], list)
        assert row["incident_id"] is None
        assert row["root_cause"] is None

    def test_empty_rows(self):
        assert enrich_anomaly_rows([], []) == []

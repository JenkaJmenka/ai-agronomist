"""Tests for cold-run row enrichment."""

from src.dashboard.services.diagnosis_enricher import (
    cold_run_agreement,
    cold_run_row_to_series,
    enrich_cold_run_rows,
    parse_cold_run_detectors,
)


def test_parse_cold_run_detectors_merged():
    assert parse_cold_run_detectors("lstm+statistical") == ["lstm", "statistical"]


def test_cold_run_agreement_from_flag():
    assert cold_run_agreement({"agreement": True, "detector": "lstm"}) is True


def test_cold_run_agreement_from_detector_label():
    assert cold_run_agreement({"detector": "lstm+statistical"}) is True
    assert cold_run_agreement({"detector": "lstm"}) is False


def test_enrich_cold_run_rows_includes_reasoning():
    rows = [
        {
            "timestamp": "2026-06-17T12:00:00+00:00",
            "detector": "statistical",
            "score": 0.8,
            "column": "measured_greenhouse_temp",
            "value": 45.0,
            "z_score": 5.2,
            "severity": "warning",
            "method": "rolling",
        }
    ]
    enriched = enrich_cold_run_rows(rows)
    assert not enriched.empty
    assert enriched.iloc[0]["reasoning"]
    assert enriched.iloc[0]["temperature"] == 45.0


def test_enrich_cold_run_rows_agreement_column():
    rows = [
        {
            "timestamp": "2026-06-17T12:00:00+00:00",
            "detector": "lstm+statistical",
            "score": 0.95,
            "column": "humidity",
            "value": 90.0,
            "agreement": True,
        }
    ]
    enriched = enrich_cold_run_rows(rows)
    assert bool(enriched.iloc[0]["agreement"]) is True


def test_cold_run_row_to_series_maps_canonical_slot():
    series = cold_run_row_to_series(
        {
            "timestamp": "2026-06-17T12:00:00+00:00",
            "detector": "statistical",
            "score": 0.5,
            "column": "measured_greenhouse_temp",
            "value": 28.0,
        }
    )
    assert series["temperature"] == 28.0
    assert series["anomaly_detectors"] == ["statistical"]

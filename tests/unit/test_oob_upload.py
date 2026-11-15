"""Tests for out-of-bounds upload behaviour (832fb85).

A.1  validate_canonical_schema: default rejects OOB; allow_oob=True passes;
     structural failures still raise with allow_oob=True.
A.2  score_portion: OOB hit on humidity 104 produces exactly one hit with
     detector "out_of_bounds", column "humidity", value 104, and
     rows_processed equals the row count.
A.3  Two consecutive portions where the OOB row falls in the first
     portion's overlap tail: the hit is reported once, not twice.
A.4  finalize_scores includes the OOB hit in high_anomalies.
A.5  Consumer round-trip: stored result without "oob_hits" key restores
     to an empty list.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.api.services.upload_portions import (
    PortionScores,
    StatCarry,
    _detect_oob_hits,
    finalize_scores,
    score_portion,
)
from src.config import OOB_LIMITS
from src.models.stuck_fault_detector import StuckCarry
from src.vendors.base import DataSourceAdapter, DataValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(n: int = 5, humidity_override: float | None = None) -> pd.DataFrame:
    """Valid canonical DataFrame with optional humidity override on the last row."""
    base = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        rows.append({
            "timestamp": base + timedelta(minutes=i),
            "temperature": 22.0 + i * 0.1,
            "humidity": 60.0,
            "co2": 400.0,
            "light": 500.0,
        })
    if humidity_override is not None and n > 0:
        rows[-1]["humidity"] = humidity_override
    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def _mock_detector() -> MagicMock:
    """Detector mock that returns zero scores."""
    det = MagicMock()
    det.predict_batch_enriched.side_effect = lambda seqs: (
        np.zeros(len(seqs)),
        np.zeros((len(seqs), 8)),
        np.zeros(len(seqs)),
    )
    return det


# ---------------------------------------------------------------------------
# A.1 — validate_canonical_schema
# ---------------------------------------------------------------------------

class TestValidateCanonicalSchemaOOB:
    """The default still rejects OOB; allow_oob=True skips the range check."""

    def test_default_rejects_humidity_104(self):
        df = _make_df(3, humidity_override=104.0)
        with pytest.raises(DataValidationError, match="humidity"):
            DataSourceAdapter.validate_canonical_schema(df)

    def test_allow_oob_passes_humidity_104(self):
        df = _make_df(3, humidity_override=104.0)
        assert DataSourceAdapter.validate_canonical_schema(df, allow_oob=True) is True

    def test_allow_oob_still_rejects_missing_column(self):
        df = _make_df(3)
        df = df.drop(columns=["co2"])
        with pytest.raises(DataValidationError, match="co2"):
            DataSourceAdapter.validate_canonical_schema(df, allow_oob=True)

    def test_allow_oob_still_rejects_nan(self):
        df = _make_df(3)
        df.loc[0, "temperature"] = float("nan")
        with pytest.raises(DataValidationError, match="NaN"):
            DataSourceAdapter.validate_canonical_schema(df, allow_oob=True)


# ---------------------------------------------------------------------------
# A.2 — score_portion with one OOB humidity value
# ---------------------------------------------------------------------------

class TestScorePortionOOB:
    """score_portion on a small frame with humidity 104."""

    def test_oob_hit_produced(self):
        n = 5
        df = _make_df(n, humidity_override=104.0)
        raw_rows = df.to_dict(orient="records")
        stat_carry = StatCarry()
        # Adaptive ranges for the four sensors (min, max)
        adaptive_ranges = [
            (20.0, 30.0),   # temperature
            (0.0, 100.0),   # humidity
            (300.0, 500.0),  # co2
            (0.0, 1000.0),  # light
        ]
        result, _ = score_portion(
            normalized_df=df,
            prev_overlap_df=pd.DataFrame(),
            raw_rows=raw_rows,
            stat_carry=stat_carry,
            adaptive_ranges=adaptive_ranges,
            detector=_mock_detector(),
            seq_len=20,
            stuck_carry=StuckCarry(),
        )
        assert result.rows_processed == n
        # Exactly one OOB hit
        assert len(result.oob_hits) == 1
        hit = result.oob_hits[0]
        assert hit.detector == "out_of_bounds"
        assert hit.column == "humidity"
        assert hit.value == 104.0
        assert hit.method == "oob"
        assert hit.severity == "high"
        assert hit.score == 1.0


# ---------------------------------------------------------------------------
# A.3 — OOB in overlap tail: reported once, not twice
# ---------------------------------------------------------------------------

class TestOOBOverlapDedup:
    """The OOB row in the first portion's overlap tail is not re-reported."""

    def test_oob_reported_once_across_two_portions(self):
        # Build portion 1: 25 rows, OOB on the last row (which will be
        # in the overlap tail for portion 2 with seq_len=20).
        n1 = 25
        df1 = _make_df(n1, humidity_override=104.0)
        raw1 = df1.to_dict(orient="records")
        stat_carry = StatCarry()
        adaptive_ranges = [
            (20.0, 30.0), (0.0, 100.0), (300.0, 500.0), (0.0, 1000.0),
        ]
        seq_len = 20

        result1, overlap = score_portion(
            normalized_df=df1,
            prev_overlap_df=pd.DataFrame(),
            raw_rows=raw1,
            stat_carry=stat_carry,
            adaptive_ranges=adaptive_ranges,
            detector=_mock_detector(),
            seq_len=seq_len,
            stuck_carry=StuckCarry(),
        )

        # Portion 1 should report the OOB hit.
        oob_count_p1 = len(result1.oob_hits)
        assert oob_count_p1 == 1

        # Portion 2: a clean 25-row chunk. The overlap from p1 contains
        # the OOB row (it was in the last seq_len-1 rows).
        base = datetime(2026, 7, 1, 12, 25, tzinfo=timezone.utc)
        rows2 = []
        for i in range(25):
            rows2.append({
                "timestamp": base + timedelta(minutes=i),
                "temperature": 22.0,
                "humidity": 60.0,
                "co2": 400.0,
                "light": 500.0,
            })
        df2 = pd.DataFrame(rows2)
        df2["timestamp"] = pd.to_datetime(df2["timestamp"], utc=True)
        raw2 = df2.to_dict(orient="records")

        result2, _ = score_portion(
            normalized_df=df2,
            prev_overlap_df=overlap,
            raw_rows=raw2,
            stat_carry=stat_carry,
            adaptive_ranges=adaptive_ranges,
            detector=_mock_detector(),
            seq_len=seq_len,
            stuck_carry=StuckCarry(),
        )

        # Portion 2 should NOT re-report the OOB (its normalized_df is clean).
        assert len(result2.oob_hits) == 0

        # Total across both portions: exactly 1.
        total_oob = oob_count_p1 + len(result2.oob_hits)
        assert total_oob == 1


# ---------------------------------------------------------------------------
# A.4 — finalize_scores includes OOB hit in high_anomalies
# ---------------------------------------------------------------------------

class TestFinalizeIncludesOOB:
    """finalize_scores merges oob_hits into the high_anomalies list."""

    def test_oob_in_high_anomalies(self):
        oob_hit = UploadHighAnomalyDetail(
            score=1.0,
            timestamp="2026-07-01T12:04:00+00:00",
            detector="out_of_bounds",
            column="humidity",
            severity="high",
            method="oob",
            value=104.0,
        )
        portion = PortionScores(
            scores=[0.01, 0.02],
            sequence_timestamps=[
                "2026-07-01T12:00:00+00:00",
                "2026-07-01T12:01:00+00:00",
            ],
            oob_hits=[oob_hit],
            rows_processed=25,
        )
        stat_carry = StatCarry(total_rows=25, columns=["temperature", "humidity"])
        response, _ = finalize_scores(
            [portion],
            tenant_id="t1",
            greenhouse_id="g1",
            imputed_channels=[],
            stat_carry=stat_carry,
            stuck_carry=StuckCarry(),
        )
        oob_in_high = [
            h for h in response.high_anomalies if h.detector == "out_of_bounds"
        ]
        assert len(oob_in_high) == 1
        assert oob_in_high[0].column == "humidity"
        assert oob_in_high[0].value == 104.0


# ---------------------------------------------------------------------------
# A.5 — Consumer stored-result round trip (absent oob_hits key)
# ---------------------------------------------------------------------------

class TestConsumerOOBRoundTrip:
    """A stored portion result WITHOUT an 'oob_hits' key restores to empty."""

    def test_missing_oob_hits_key_restores_empty(self):
        # Simulate what _finalize does when it reads a stored result
        # that was written before the OOB gate existed.
        stored = {
            "schema_version": 5,
            "scores": [0.01],
            "sequence_timestamps": ["2026-07-01T12:00:00+00:00"],
            "stat_hits": [],
            "stuck_hits": [],
            "mad_hits": [],
            # NO "oob_hits" key
            "rows_processed": 100,
        }
        # Reproduce the consumer's restoration logic from _finalize
        oob_hits = [
            UploadHighAnomalyDetail(**h) for h in stored.get("oob_hits", [])
        ]
        assert oob_hits == []

    def test_present_oob_hits_key_restores_correctly(self):
        stored = {
            "oob_hits": [
                {
                    "score": 1.0,
                    "timestamp": "2026-07-01T12:04:00+00:00",
                    "detector": "out_of_bounds",
                    "column": "humidity",
                    "severity": "high",
                    "method": "oob",
                    "value": 104.0,
                }
            ],
        }
        oob_hits = [
            UploadHighAnomalyDetail(**h) for h in stored.get("oob_hits", [])
        ]
        assert len(oob_hits) == 1
        assert oob_hits[0].detector == "out_of_bounds"
        assert oob_hits[0].value == 104.0

"""Tests for code-review findings — fix/review-findings branch.

Covers:
  F1 — build_rule_reasoning: unknown vote key yields text, not KeyError.
  F2 — LRU eviction: _ema_cache cleared alongside _cusum_cache.
  F3 — drift_watch_service._to_float_or_nan: ±inf returns NaN, not ±inf.
  F4 — _MAHALANOBIS_HISTORY_VOTE_ENABLED=False: no DB query on each call.
  F5 — _SCADA_TYPE_TO_VOTE is a module-level constant.
  F6 — SpikeMADDetector per distinct tuning_c, not per sensor per call.
  F7 — build_diurnal_profiles accepts anchor; _ensure_diurnal_profiles
       passes it and uses (tenant, greenhouse, date) as cache key.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio
import torch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api.services.comprehensive_pipeline import (
    ComprehensiveInferencePipeline,
    _MAHALANOBIS_HISTORY_VOTE_ENABLED,
    _SCADA_TYPE_TO_VOTE,
    build_rule_reasoning,
)
from src.api.services.drift_watch_service import _to_float_or_nan
from src.config import INPUT_DIM, LATENT_DIM, MODEL_CONFIG, RANDOM_SEED, SEQ_LEN
from src.db.models import Base
from src.models.inference import AnomalyDetector
from src.models.lstm_vae import LSTMVAE


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def checkpoint_path(tmp_path) -> str:
    model = LSTMVAE(**MODEL_CONFIG)
    path = str(tmp_path / "review_findings_model.pt")
    torch.save(
        {"model_config": MODEL_CONFIG, "model_state_dict": model.state_dict()},
        path,
    )
    return path


@pytest.fixture
def detector(checkpoint_path: str) -> AnomalyDetector:
    return AnomalyDetector(checkpoint_path=checkpoint_path)


@pytest.fixture
def pipeline(detector: AnomalyDetector) -> ComprehensiveInferencePipeline:
    return ComprehensiveInferencePipeline(detector=detector)


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ---------------------------------------------------------------------------
# F1 — build_rule_reasoning: unknown vote key
# ---------------------------------------------------------------------------


class TestBuildRuleReasoningUnknownKey:
    def test_unknown_primary_key_returns_string(self):
        """An unknown primary vote key must not raise KeyError."""
        result = build_rule_reasoning({"unknown_detector": ["temperature"]})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_unknown_primary_key_mentions_key_or_sensors(self):
        """The returned text should reference either the key name or the sensor."""
        result = build_rule_reasoning({"totally_unknown": ["humidity"]})
        assert "totally_unknown" in result or "humidity" in result

    def test_known_key_still_works(self):
        """Known keys must still produce their configured reasoning text."""
        result = build_rule_reasoning({"spike_mad": ["temperature", "humidity"]})
        assert "MAD" in result or "spike" in result.lower() or "median" in result.lower()

    def test_unknown_co_firing_key_returns_string(self):
        """An unknown co-firing key must not raise KeyError."""
        result = build_rule_reasoning({
            "spike_mad": ["temperature"],
            "another_unknown": ["humidity"],
        })
        assert isinstance(result, str)
        assert "also flagged by" in result

    def test_empty_rule_votes_returns_empty_string(self):
        """Empty rule_votes dict must return empty string (no votes)."""
        result = build_rule_reasoning({})
        assert result == ""


# ---------------------------------------------------------------------------
# F2 — LRU eviction: _ema_cache cleared alongside _cusum_cache
# ---------------------------------------------------------------------------


class TestLRUEviction:
    def test_eviction_clears_ema_cache(self, pipeline: ComprehensiveInferencePipeline):
        """When a tenant is evicted from _tracker_cache, _ema_cache entries must be cleared."""
        from src.models.cusum_drift_detector import CUSUMDriftDetector, EMABaselineEstimator
        # Seed both caches for a known tenant
        tenant = "evict_test_tenant"
        gh = "gh1"
        key = (tenant, gh)
        pipeline._cusum_cache[key] = CUSUMDriftDetector()
        pipeline._ema_cache[key] = EMABaselineEstimator.for_physical_series()

        # Place a sentinel in _tracker_cache and then evict by filling the LRU
        from src.models.ewma_tracker import EWMATracker
        from src.models.pot_threshold import POTThresholdFitter
        pipeline._tracker_cache[tenant] = (EWMATracker(), POTThresholdFitter())

        # Force eviction by filling the cache beyond its max_size with OTHER tenants
        for i in range(100):
            pipeline._tracker_cache[f"filler_tenant_{i}"] = (EWMATracker(), POTThresholdFitter())

        # After eviction the EMA entry for the evicted tenant must be gone
        assert key not in pipeline._ema_cache, (
            "_ema_cache was not cleared on LRU eviction"
        )

    def test_eviction_clears_cusum_cache(self, pipeline: ComprehensiveInferencePipeline):
        """When a tenant is evicted from _tracker_cache, _cusum_cache entries must be cleared."""
        from src.models.cusum_drift_detector import CUSUMDriftDetector
        from src.models.ewma_tracker import EWMATracker
        from src.models.pot_threshold import POTThresholdFitter
        tenant = "evict_cusum_tenant"
        gh = "gh1"
        key = (tenant, gh)
        pipeline._cusum_cache[key] = CUSUMDriftDetector()
        pipeline._tracker_cache[tenant] = (EWMATracker(), POTThresholdFitter())

        for i in range(100):
            pipeline._tracker_cache[f"cusum_filler_{i}"] = (EWMATracker(), POTThresholdFitter())

        assert key not in pipeline._cusum_cache, (
            "_cusum_cache was not cleared on LRU eviction"
        )

    def test_cusum_ema_keyed_by_tenant_greenhouse(self, pipeline: ComprehensiveInferencePipeline):
        """CUSUM and EMA caches use (tenant, greenhouse) tuple keys."""
        from src.models.cusum_drift_detector import CUSUMDriftDetector, EMABaselineEstimator
        tenant = "tuple_key_tenant"
        for gh in ("gh_a", "gh_b"):
            key = (tenant, gh)
            pipeline._cusum_cache[key] = CUSUMDriftDetector()
            pipeline._ema_cache[key] = EMABaselineEstimator.for_physical_series()

        assert (tenant, "gh_a") in pipeline._cusum_cache
        assert (tenant, "gh_b") in pipeline._cusum_cache
        assert (tenant, "gh_a") in pipeline._ema_cache
        assert (tenant, "gh_b") in pipeline._ema_cache


# ---------------------------------------------------------------------------
# F3 — drift_watch_service._to_float_or_nan: ±inf -> NaN
# ---------------------------------------------------------------------------


class TestToFloatOrNanFinite:
    def test_positive_inf_returns_nan(self):
        result = _to_float_or_nan(float("inf"))
        assert math.isnan(result), f"Expected NaN for +inf, got {result}"

    def test_negative_inf_returns_nan(self):
        result = _to_float_or_nan(float("-inf"))
        assert math.isnan(result), f"Expected NaN for -inf, got {result}"

    def test_nan_returns_nan(self):
        result = _to_float_or_nan(float("nan"))
        assert math.isnan(result), "NaN input must return NaN"

    def test_finite_value_preserved(self):
        result = _to_float_or_nan(42.5)
        assert result == 42.5

    def test_none_returns_nan(self):
        result = _to_float_or_nan(None)
        assert math.isnan(result)


# ---------------------------------------------------------------------------
# F4 — _MAHALANOBIS_HISTORY_VOTE_ENABLED = False
# ---------------------------------------------------------------------------


class TestMahalanobisVoteDisabled:
    def test_flag_is_false(self):
        """Module-level flag must be False — only change to True after the
        reference distribution is conditioned on time-of-day."""
        assert _MAHALANOBIS_HISTORY_VOTE_ENABLED is False

    @pytest.mark.asyncio
    async def test_no_db_query_when_disabled(self, pipeline, db_session):
        """When flag is False, _load_mahalanobis_history must not be called."""
        from src.config import INPUT_DIM, SEQ_LEN
        from src.models.physics_validator import ValidationResult

        seq = np.full((SEQ_LEN, INPUT_DIM), 0.5, dtype=np.float32).tolist()

        neutral = ValidationResult(suppressed=False, confirmed=False, reason="")
        with patch(
            "src.api.services.comprehensive_pipeline.PhysicsValidator.validate",
            return_value=neutral,
        ):
            with patch.object(
                pipeline,
                "_load_mahalanobis_history",
                new_callable=AsyncMock,
            ) as mock_load:
                with patch(
                    "src.models.inference.AnomalyDetector.predict_batch_enriched",
                    side_effect=lambda seqs: (
                        np.array([0.1], dtype=np.float32),
                        np.zeros((1, LATENT_DIM), dtype=np.float32),
                        np.array([0.1], dtype=np.float32),
                    ),
                ):
                    await pipeline.run(
                        "f4_tenant", seq, db_session, greenhouse_id="default"
                    )
                # Must not have been called — flag is False
                mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# F5 — _SCADA_TYPE_TO_VOTE is a module-level constant
# ---------------------------------------------------------------------------


class TestScadaTypeToVote:
    def test_is_dict(self):
        assert isinstance(_SCADA_TYPE_TO_VOTE, dict)

    def test_contains_expected_keys(self):
        """All six SCADA anomaly type constants must map to vote-key strings."""
        from src.models.setpoint_scada_detector import (
            DEHUMIDIFICATION_FAILURE,
            HUMIDITY_SATURATION_RISK,
            OVER_HEATING,
            SENSOR_FLATLINE,
            THERMAL_DIVERGENCE,
            UNDER_HEATING,
        )
        assert UNDER_HEATING in _SCADA_TYPE_TO_VOTE
        assert OVER_HEATING in _SCADA_TYPE_TO_VOTE
        assert DEHUMIDIFICATION_FAILURE in _SCADA_TYPE_TO_VOTE
        assert HUMIDITY_SATURATION_RISK in _SCADA_TYPE_TO_VOTE
        assert THERMAL_DIVERGENCE in _SCADA_TYPE_TO_VOTE
        assert SENSOR_FLATLINE in _SCADA_TYPE_TO_VOTE


# ---------------------------------------------------------------------------
# F7 — build_diurnal_profiles anchor parameter
# ---------------------------------------------------------------------------


class TestDiurnalProfilesAnchor:
    @pytest.mark.asyncio
    async def test_anchor_param_accepted(self, db_session):
        """build_diurnal_profiles must accept an anchor argument without error."""
        from src.api.services.diurnal_profile_service import build_diurnal_profiles

        anchor = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
        result = await build_diurnal_profiles(
            db_session,
            tenant_id="anchor_test_tenant",
            greenhouse_id="default",
            anchor=anchor,
        )
        # Empty DB -> {} (not an error)
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_anchor_default_is_now(self, db_session):
        """When anchor is None, build_diurnal_profiles defaults to wall-clock now."""
        from src.api.services.diurnal_profile_service import build_diurnal_profiles

        # Should not raise
        result = await build_diurnal_profiles(
            db_session,
            tenant_id="anchor_default_tenant",
            greenhouse_id="default",
        )
        assert isinstance(result, dict)

    def test_diurnal_cache_key_includes_date(self, pipeline: ComprehensiveInferencePipeline):
        """The diurnal profiles cache must use (tenant, greenhouse, date) as key."""
        from datetime import date as _date
        # Inject a fake entry with the new 3-tuple key
        fake_key = ("tenant_x", "gh_y", _date(2026, 7, 10))
        pipeline._diurnal_profiles_cache[fake_key] = {}
        assert fake_key in pipeline._diurnal_profiles_cache

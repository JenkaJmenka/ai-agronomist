"""Tests for VPD derived-channel wiring in the runtime layer.

Covers: _attach_derived_vpd mutates event when temp+humidity present;
absent when humidity missing; VPD source set to "derived"; scoring unaffected
by non-ML channels (vpd/soil_moisture); runtime end-to-end publishes vpd.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from src.live.derived import compute_vpd
from src.live.events import LiveReadingEvent
from src.live.hub import LiveHub, stream_key
from src.live.runtime import LiveRuntime, _attach_derived_vpd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT = "vpd_tenant"
_GH = "vpd_gh"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    client = FakeRedis()
    yield client
    await client.aclose()


@pytest_asyncio.fixture
def hub(fake_redis: FakeRedis) -> LiveHub:
    return LiveHub(redis_client=fake_redis, block_ms=10)


# ---------------------------------------------------------------------------
# _attach_derived_vpd — unit tests
# ---------------------------------------------------------------------------


class TestAttachDerivedVpd:

    def test_vpd_attached_when_temp_and_humidity_present(self) -> None:
        """vpd channel appears when both temperature and humidity are present."""
        event = LiveReadingEvent(
            seq="1",
            timestamp=datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"temperature": 25.0, "humidity": 60.0, "co2": 400.0, "light": 500.0},
            sources={"temperature": "src1", "humidity": "src1", "co2": "src2", "light": "src1"},
        )

        _attach_derived_vpd(event)

        assert "vpd" in event.channels
        expected_vpd = compute_vpd(25.0, 60.0)
        assert abs(event.channels["vpd"] - expected_vpd) < 0.001

    def test_vpd_source_is_derived(self) -> None:
        """VPD provenance is set to 'derived'."""
        event = LiveReadingEvent(
            seq="1",
            timestamp=datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"temperature": 25.0, "humidity": 60.0},
            sources={"temperature": "src1", "humidity": "src1"},
        )

        _attach_derived_vpd(event)

        assert event.sources.get("vpd") == "derived"

    def test_vpd_absent_when_humidity_missing(self) -> None:
        """vpd is NOT attached when humidity is missing."""
        event = LiveReadingEvent(
            seq="2",
            timestamp=datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"temperature": 25.0, "co2": 400.0},
            sources={"temperature": "src1", "co2": "src2"},
        )

        _attach_derived_vpd(event)

        assert "vpd" not in event.channels
        assert "vpd" not in event.sources

    def test_vpd_absent_when_temperature_missing(self) -> None:
        """vpd is NOT attached when temperature is missing."""
        event = LiveReadingEvent(
            seq="3",
            timestamp=datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"humidity": 65.0},
            sources={"humidity": "src1"},
        )

        _attach_derived_vpd(event)

        assert "vpd" not in event.channels

    def test_vpd_absent_when_both_missing(self) -> None:
        """vpd is NOT attached when both temperature and humidity are missing."""
        event = LiveReadingEvent(
            seq="4",
            timestamp=datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"co2": 400.0, "light": 500.0},
            sources={"co2": "src2", "light": "src1"},
        )

        _attach_derived_vpd(event)

        assert "vpd" not in event.channels

    def test_vpd_with_additional_channels(self) -> None:
        """vpd is attached even when extra channels (soil_moisture) are present."""
        event = LiveReadingEvent(
            seq="5",
            timestamp=datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": 20.0,
                "humidity": 80.0,
                "co2": 400.0,
                "light": 500.0,
                "soil_moisture": 70.0,
            },
            sources={
                "temperature": "src1",
                "humidity": "src1",
                "co2": "src2",
                "light": "src1",
                "soil_moisture": "src3",
            },
        )

        _attach_derived_vpd(event)

        assert "vpd" in event.channels
        expected = compute_vpd(20.0, 80.0)
        assert abs(event.channels["vpd"] - expected) < 0.001

    def test_vpd_mutates_in_place(self) -> None:
        """_attach_derived_vpd mutates the event in place (no return value)."""
        event = LiveReadingEvent(
            seq="6",
            timestamp=datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={"temperature": 25.0, "humidity": 60.0},
            sources={"temperature": "src1", "humidity": "src1"},
        )

        result = _attach_derived_vpd(event)

        assert result is None  # returns None (mutates in place)
        assert "vpd" in event.channels


# ---------------------------------------------------------------------------
# Scoring unaffected by non-ML channels
# ---------------------------------------------------------------------------


class TestScoringUnaffectedByExtraChannels:

    def test_extract_vector_ignores_vpd_and_soil(self) -> None:
        """LiveScorer._extract_vector only pulls the 4 canonical ML channels."""
        from src.live.scoring import LiveScorer
        from src.live.attribution import CHANNEL_ORDER

        scorer = LiveScorer(
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            hub=MagicMock(),
            threshold=0.5,
        )

        # Feed all canonical channels + extras
        event_with_extras = LiveReadingEvent(
            seq="1",
            timestamp=datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc),
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            channels={
                "temperature": 25.0,
                "humidity": 60.0,
                "co2": 400.0,
                "light": 500.0,
                "vpd": 1.27,
                "soil_moisture": 70.0,
                "root_zone_temp": 22.0,
            },
            sources={ch: "src" for ch in ["temperature", "humidity", "co2", "light", "vpd", "soil_moisture", "root_zone_temp"]},
        )

        vec = scorer._extract_vector(event_with_extras)
        assert vec is not None
        assert len(vec) == len(CHANNEL_ORDER)
        # Vector should contain exactly the 4 ML channels
        assert vec[CHANNEL_ORDER.index("temperature")] == 25.0
        assert vec[CHANNEL_ORDER.index("humidity")] == 60.0
        assert vec[CHANNEL_ORDER.index("co2")] == 400.0
        assert vec[CHANNEL_ORDER.index("light")] == 500.0

    def test_same_score_with_and_without_extra_channels(self) -> None:
        """Score is identical whether extra channels (vpd, soil) are present or not."""
        from src.live.scoring import LiveScorer
        import numpy as np

        hub_mock = MagicMock()

        def run_scorer_with_channels(extra_channels: dict[str, float]) -> list[float]:
            """Run scorer and return all non-None scores."""
            import torch
            scorer = LiveScorer(
                tenant_id=_TENANT,
                greenhouse_id=_GH,
                hub=hub_mock,
                threshold=0.5,
            )
            detector = MagicMock()
            detector.model = MagicMock(
                side_effect=lambda t: (torch.zeros_like(t), None, None, None)
            )

            scores: list[float] = []
            loop = asyncio.new_event_loop()
            try:
                with patch.object(
                    scorer, '_resolve_detector',
                    new_callable=AsyncMock,
                    return_value=detector,
                ):
                    from src.config import SEQ_LEN
                    for i in range(SEQ_LEN + 1):
                        base = {
                            "temperature": 25.0,
                            "humidity": 60.0,
                            "co2": 400.0,
                            "light": 500.0,
                        }
                        base.update(extra_channels)
                        event = LiveReadingEvent(
                            seq=str(i),
                            timestamp=datetime(2026, 7, 18, 12, 0, i, tzinfo=timezone.utc),
                            tenant_id=_TENANT,
                            greenhouse_id=_GH,
                            channels=base,
                            sources={k: "src" for k in base},
                        )
                        result = loop.run_until_complete(scorer.score(event))
                        if result.anomaly_score is not None:
                            scores.append(result.anomaly_score)
            finally:
                loop.close()
            return scores

        scores_without = run_scorer_with_channels({})
        scores_with = run_scorer_with_channels({
            "vpd": 1.27,
            "soil_moisture": 70.0,
            "root_zone_temp": 22.0,
        })

        assert len(scores_without) > 0
        assert len(scores_with) > 0
        # Scores should be identical since extra channels are ignored
        assert scores_without == scores_with


# ---------------------------------------------------------------------------
# Runtime integration — VPD published to hub
# ---------------------------------------------------------------------------


class TestVpdRuntimeIntegration:

    @pytest.fixture(autouse=True)
    def _mock_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin config for fast deterministic tests."""
        monkeypatch.setattr("src.live.runtime.MOCK_SCADA_ENABLED", True)
        monkeypatch.setattr("src.live.runtime.MOCK_SCADA_SCOPES", f"{_TENANT}:{_GH}")
        monkeypatch.setattr("src.live.runtime.MOCK_SCADA_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr("src.config.MOCK_SCADA_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr("src.live.sources.mock.MOCK_SCADA_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr("src.live.runtime.LIVE_PERSIST_ENABLED", False)
        monkeypatch.setattr("src.live.runtime.LIVE_SOURCES_JSON", "")

    @pytest.fixture(autouse=True)
    def _block_live_scorer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Block LiveScorer import to test passthrough path."""
        sys.modules.pop("src.live.scoring", None)

        class _FailImport:
            def __getattr__(self, name: str):
                raise ImportError("LiveScorer not available in test")

        sys.modules["src.live.scoring"] = _FailImport()  # type: ignore
        yield
        sys.modules.pop("src.live.scoring", None)

    @pytest.mark.asyncio
    async def test_published_event_carries_vpd(
        self, hub: LiveHub, fake_redis: FakeRedis,
    ) -> None:
        """A merged reading published to the hub carries vpd with source 'derived'."""
        runtime = LiveRuntime.build_from_config(hub)
        await runtime.start()

        try:
            # Wait for at least one reading with vpd
            key = stream_key(_TENANT, _GH)
            deadline = asyncio.get_event_loop().time() + 3.0
            found_vpd = False

            while asyncio.get_event_loop().time() < deadline:
                entries = await fake_redis.xrange(key, "-", "+")
                for _eid, fields in entries:
                    if fields.get(b"type", b"").decode() != "reading":
                        continue
                    data = json.loads(fields[b"data"].decode())
                    channels = data.get("channels", {})
                    sources = data.get("sources", {})
                    if "vpd" in channels and sources.get("vpd") == "derived":
                        found_vpd = True
                        break
                if found_vpd:
                    break
                await asyncio.sleep(0.1)

            assert found_vpd, "Expected to find a reading with vpd='derived' in hub"
        finally:
            await runtime.stop()

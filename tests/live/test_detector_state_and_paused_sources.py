"""Live scopes must not look healthy when they cannot score, and a paused
scope's sources must not report ``online``.

Covers the two observable halves of the no-detector fix:

* ``LiveRuntime.source_statuses`` reports ``paused`` for a stopped scope.
  ``MergeBuffer`` seeds every source to ``online`` at construction
  (``src/live/merge.py:116``) and only revises it from inside the merger loop,
  so a stopped scope used to report every source ``online`` forever with a
  ``last_seen`` frozen at the pause — which an operator read as a live feed.
* ``LiveRuntime.detector_state`` / ``LiveScorer.detector_state`` surface
  whether the scope can score at all, so ``/api/live/status`` can say
  "live, not analysed" instead of looking fine.

Plus the positive case: a scope WITH a resolvable checkpoint actually scores.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Optional

import pytest
import pytest_asyncio
import torch
from fakeredis.aioredis import FakeRedis

from src.config import (
    BETA,
    DROPOUT_RATE,
    HIDDEN_DIMS,
    INPUT_DIM,
    LATENT_DIM,
    SEQ_LEN,
)
from src.live.detector_resolver import (
    DETECTOR_STATE_MISSING,
    DETECTOR_STATE_READY,
    DETECTOR_STATE_UNKNOWN,
    DetectorResolver,
)
from src.live.events import LiveReadingEvent
from src.live.hub import LiveHub
from src.live.runtime import LiveRuntime
from src.live.runtime_builder import _build_scope
from src.live.scope_control import clear_all_suppressed_scopes
from src.live.scoring import LiveScorer
from src.live.sources.base import SourceConfig
from src.live.sources.mock import MockScadaSource

_TENANT = "tenant_detstate"
_GH = "gh_A"

# The checkpoint below is built from freshly initialised weights, so without a
# seed every run scores against a different model. Pinned (to the repo's usual
# 42, also src.config.RANDOM_SEED) so the scores these tests compare are
# reproducible rather than whatever global RNG state the process happened to
# be in. Seeded under fork_rng so the global stream is left untouched for
# every test that runs after this module.
_CHECKPOINT_SEED = 42

# A steady in-range reading: TEMP_RANGE is 20-30 C, so this normalises to 0.25.
_NORMAL_TEMP_C = 22.5
# Clearly anomalous against the 20-30 C range (normalises to 2.5) yet a reading
# a real sensor genuinely reports: a greenhouse with failed ventilation on a hot
# day reaches it. Deliberately not a value chosen for how badly the model
# reconstructs it -- an input far enough out of range to overflow float32 would
# make the comparison below a property of the overflow, not of the scorer.
_ANOMALOUS_TEMP_C = 45.0


def _mock_config(poll_interval: float = 0.02) -> SourceConfig:
    return SourceConfig(
        source_id=f"{_TENANT}_{_GH}_mock_climate",
        vendor="mock",
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        channels=["temperature", "humidity", "co2", "light"],
        priority=10,
        poll_interval_seconds=poll_interval,
        params={"seed": 42, "demo_mode": False},
    )


class _NullSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _null_session_factory():
    return _NullSession()


@pytest.fixture(autouse=True)
def _clean_gate() -> AsyncGenerator[None, None]:
    clear_all_suppressed_scopes()
    yield
    clear_all_suppressed_scopes()


@pytest_asyncio.fixture
async def runtime() -> AsyncGenerator[LiveRuntime, None]:
    redis = FakeRedis()
    hub = LiveHub(redis_client=redis, block_ms=50)
    # The real scope builder, so source_statuses sees a production merger.
    scope = _build_scope(hub, _TENANT, _GH, [MockScadaSource(_mock_config())], None)
    rt = LiveRuntime(hub, [scope], None)
    await rt.start()
    try:
        yield rt
    finally:
        await rt.stop()
        close_fn = getattr(redis, "aclose", None) or redis.close
        await close_fn()


class TestPausedScopeSources:
    @pytest.mark.asyncio
    async def test_stopped_scope_reports_no_online_source(
        self, runtime: LiveRuntime
    ) -> None:
        """After Go Live then pause, no source may report 'online'."""
        assert await runtime.start_scope(_TENANT, _GH) is True
        live = runtime.source_statuses(_TENANT, _GH)
        assert live, "expected at least one source while live"

        await runtime.stop_scope(_TENANT, _GH)
        paused = runtime.source_statuses(_TENANT, _GH)

        assert paused, "a paused scope must still list its sources"
        assert [entry["status"] for entry in paused] == ["paused"] * len(paused), (
            f"a paused scope must not report 'online': {paused}"
        )

    @pytest.mark.asyncio
    async def test_idle_scope_before_first_go_live_is_paused(
        self, runtime: LiveRuntime
    ) -> None:
        """A configured scope that was never started is paused, not online."""
        statuses = runtime.source_statuses(_TENANT, _GH)
        assert statuses, "a configured scope lists sources even while idle"
        assert all(entry["status"] == "paused" for entry in statuses), statuses

    @pytest.mark.asyncio
    async def test_running_scope_reports_real_merger_health(
        self, runtime: LiveRuntime
    ) -> None:
        """The override applies only while stopped — a live scope is untouched."""
        assert await runtime.start_scope(_TENANT, _GH) is True
        statuses = runtime.source_statuses(_TENANT, _GH)
        assert statuses
        assert all(entry["status"] != "paused" for entry in statuses), statuses

    @pytest.mark.asyncio
    async def test_unknown_scope_still_returns_empty(
        self, runtime: LiveRuntime
    ) -> None:
        assert runtime.source_statuses("nope", "nope") == []


class TestRuntimeDetectorState:
    @pytest.mark.asyncio
    async def test_unconfigured_scope_is_unknown(self, runtime: LiveRuntime) -> None:
        assert runtime.detector_state("nope", "nope") == DETECTOR_STATE_UNKNOWN

    @pytest.mark.asyncio
    async def test_configured_scope_reports_its_resolver_state(
        self, runtime: LiveRuntime
    ) -> None:
        """detector_state delegates to the scope's scorer, not a guess."""
        state = runtime.detector_state(_TENANT, _GH)
        assert state in {
            DETECTOR_STATE_UNKNOWN,
            DETECTOR_STATE_READY,
            DETECTOR_STATE_MISSING,
        }


def _write_checkpoint(path: str) -> str:
    """A real, loadable LSTM-VAE checkpoint in the shape inference.py expects.

    Weights are untrained but seeded with ``_CHECKPOINT_SEED``, so every run
    scores against the same model.
    """
    from src.models.lstm_vae import LSTMVAE

    cfg = dict(
        input_dim=INPUT_DIM,
        hidden_dims=HIDDEN_DIMS,
        latent_dim=LATENT_DIM,
        seq_len=SEQ_LEN,
        beta=BETA,
        dropout=DROPOUT_RATE,
    )
    with torch.random.fork_rng():
        torch.manual_seed(_CHECKPOINT_SEED)
        model = LSTMVAE(**cfg)
    torch.save({"model_config": cfg, "model_state_dict": model.state_dict()}, path)
    return path


def _event(ts: datetime, *, temperature: float) -> LiveReadingEvent:
    return LiveReadingEvent(
        seq="",
        timestamp=ts,
        tenant_id=_TENANT,
        greenhouse_id=_GH,
        channels={
            "temperature": temperature,
            "humidity": 65.0,
            "co2": 420.0,
            "light": 300.0,
        },
        sources={ch: "mock" for ch in ("temperature", "humidity", "co2", "light")},
    )


class _NoEnrichment:
    """No-op stand-in for ``EnrichmentRunner`` via the ``enrichment=`` DI seam.

    Enrichment is not under test here, and it must not run: it mutates the
    detector these tests are scoring with, concurrently with the scoring pass.
    ``EnrichmentRunner._run_enrichment`` builds an ``AnomalyAttributor``, whose
    ``__init__`` calls ``model.to(device)`` on the **shared** cached detector
    (``src/models/anomaly_attributor.py:79``). ``RNNBase._apply`` blanks
    ``_flat_weight_refs`` for the duration of that call
    (``torch/nn/modules/rnn.py:298``), so a forward pass running at the same
    time in ``asyncio.to_thread`` reaches ``_weights_have_changed`` with an
    empty ref list and raises ``ValueError: zip() argument 2 is longer than
    argument 1``. ``LiveScorer.score`` logs "Forward pass failed" and returns
    the event **unscored**, which the assertions below then see as
    ``anomaly_score is None``.

    Both tests in this class cross ``LIVE_ANOMALY_THRESHOLD`` even on the
    in-range window, so the real runner would spawn enrichment on every run and
    the race was lost in roughly 2 % of them -- 5/200 seeds for this test as it
    stood on ``main``, 3/200 for the same logic at the 45 C input now used,
    versus 0/400 with this stub. The production defect is reported separately;
    the tests here are about scoring, so they take the seam.
    """

    def spawn(self, window_array: Any, anomaly_seq: str) -> None:
        return None

    async def close(self) -> None:
        return None


class TestScopeWithResolvableModelScores:
    """A live scope whose model resolves must actually score its readings."""

    @staticmethod
    def _scorer(tmp_path, hub: LiveHub) -> LiveScorer:
        checkpoint = _write_checkpoint(str(tmp_path / "live.pt"))

        async def resolve_checkpoint_path(session, tenant_id):  # noqa: ANN001
            return checkpoint

        resolver = DetectorResolver(
            _TENANT,
            session_factory=_null_session_factory,
            checkpoint_resolver=resolve_checkpoint_path,
        )
        return LiveScorer(
            tenant_id=_TENANT,
            greenhouse_id=_GH,
            hub=hub,
            resolver=resolver,
            enrichment=_NoEnrichment(),
        )

    @pytest.mark.asyncio
    async def test_scores_appear_once_the_window_is_warm(self, tmp_path) -> None:
        redis = FakeRedis()
        hub = LiveHub(redis_client=redis, block_ms=50)
        scorer = self._scorer(tmp_path, hub)
        try:
            base = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
            scores: list[Optional[float]] = []
            for i in range(SEQ_LEN + 3):
                scored = await scorer.score(
                    _event(
                        base + timedelta(seconds=2 * i),
                        temperature=_NORMAL_TEMP_C,
                    )
                )
                scores.append(scored.anomaly_score)

            assert all(s is None for s in scores[: SEQ_LEN - 1]), (
                "nothing may be scored before the window is full"
            )
            assert scores[-1] is not None, (
                "a scope with a resolvable checkpoint must score once warm — "
                "this is the whole point of the fix"
            )
            assert scorer.detector_state == DETECTOR_STATE_READY
        finally:
            await scorer.close()
            close_fn = getattr(redis, "aclose", None) or redis.close
            await close_fn()

    @pytest.mark.asyncio
    async def test_anomalous_input_scores_above_normal_input(self, tmp_path) -> None:
        """Relative, not absolute: the checkpoint's weights are untrained, so a
        threshold assertion would be meaningless, but an out-of-range reading
        must still reconstruct worse than an in-range one."""
        redis = FakeRedis()
        hub = LiveHub(redis_client=redis, block_ms=50)
        scorer = self._scorer(tmp_path, hub)
        try:
            base = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
            for i in range(SEQ_LEN):
                await scorer.score(
                    _event(
                        base + timedelta(seconds=2 * i),
                        temperature=_NORMAL_TEMP_C,
                    )
                )
            normal = await scorer.score(
                _event(
                    base + timedelta(seconds=2 * SEQ_LEN),
                    temperature=_NORMAL_TEMP_C,
                )
            )
            # Drive the window out of the 20-30C sensor range.
            for i in range(SEQ_LEN):
                spiked = await scorer.score(
                    _event(
                        base + timedelta(seconds=2 * (SEQ_LEN + 1 + i)),
                        temperature=_ANOMALOUS_TEMP_C,
                    )
                )

            # Finiteness first, and explicitly: an unscored event (None) or an
            # overflowed one (inf/NaN) must fail here, naming the cause, rather
            # than reaching the comparison below as a bare "None is not None".
            assert normal.anomaly_score is not None, (
                "the in-range window must be scored once warm"
            )
            assert spiked.anomaly_score is not None, (
                "the out-of-range window must be scored, not dropped as "
                "non-finite -- see LiveScorer.score's isfinite guard"
            )
            assert math.isfinite(normal.anomaly_score), normal.anomaly_score
            assert math.isfinite(spiked.anomaly_score), spiked.anomaly_score

            assert spiked.anomaly_score > normal.anomaly_score, (
                f"out-of-range window should score higher: "
                f"{spiked.anomaly_score} vs {normal.anomaly_score}"
            )
        finally:
            await scorer.close()
            close_fn = getattr(redis, "aclose", None) or redis.close
            await close_fn()

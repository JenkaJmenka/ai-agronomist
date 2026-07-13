"""Unit tests for replay buffers (Gap 3 — Online Learning)."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from src.models.replay_buffer import (
    UNLABELED,
    AnomalyBuffer,
    ReplayBuffer,
    SeasonalBuffer,
)

pytestmark = pytest.mark.unit

SEQ_SHAPE = (5, 3)


def make_sequence(fill: float = 1.0) -> np.ndarray:
    """Small identifiable sequence (constant fill value)."""
    return np.full(SEQ_SHAPE, fill, dtype=np.float32)


# ---------------------------------------------------------------------------
# ReplayBuffer — add / len / metadata defaults
# ---------------------------------------------------------------------------


class TestReplayBufferAdd:
    def test_empty_buffer_has_zero_length(self) -> None:
        assert len(ReplayBuffer(capacity=10)) == 0

    def test_add_increments_length(self) -> None:
        buffer = ReplayBuffer(capacity=10)
        buffer.add(make_sequence())
        buffer.add(make_sequence())
        assert len(buffer) == 2

    def test_add_defaults_loss_zero_and_unlabeled(self) -> None:
        buffer = ReplayBuffer(capacity=10)
        buffer.add(make_sequence())
        assert buffer.losses[0] == 0.0
        assert buffer.labels[0] == UNLABELED
        assert buffer.timestamps[0] > 0

    def test_add_stores_explicit_metadata(self) -> None:
        buffer = ReplayBuffer(capacity=10)
        buffer.add(make_sequence(), loss=0.42, timestamp=123.0, label="normal")
        assert buffer.losses[0] == pytest.approx(0.42)
        assert buffer.timestamps[0] == pytest.approx(123.0)
        assert buffer.labels[0] == "normal"

    def test_add_batch_adds_all_sequences(self) -> None:
        buffer = ReplayBuffer(capacity=10)
        batch = np.random.rand(4, *SEQ_SHAPE).astype(np.float32)
        buffer.add_batch(batch)
        assert len(buffer) == 4

    def test_fifo_eviction_at_capacity(self) -> None:
        buffer = ReplayBuffer(capacity=3)
        for i in range(5):
            buffer.add(make_sequence(float(i)), loss=float(i))
        assert len(buffer) == 3
        # Oldest (0, 1) evicted; (2, 3, 4) retained.
        assert list(buffer.losses) == [2.0, 3.0, 4.0]

    def test_capacity_warning_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        buffer = ReplayBuffer(capacity=2)
        with caplog.at_level(logging.WARNING, logger="src.models.replay_buffer"):
            buffer.add(make_sequence())
            buffer.add(make_sequence())  # Reaches capacity -> warns.
            buffer.add(make_sequence())  # Still at capacity -> silent.
        warnings = [r for r in caplog.records if "at capacity" in r.getMessage()]
        assert len(warnings) == 1
        assert buffer._capacity_warned is True


# ---------------------------------------------------------------------------
# ReplayBuffer — sampling
# ---------------------------------------------------------------------------


class TestReplayBufferSampling:
    def test_sample_returns_all_when_fewer_than_batch_size(self) -> None:
        buffer = ReplayBuffer(capacity=10)
        for _ in range(3):
            buffer.add(make_sequence())
        batch = buffer.sample(batch_size=8)
        assert batch.shape == (3, *SEQ_SHAPE)

    def test_sample_returns_exact_batch_size(self) -> None:
        buffer = ReplayBuffer(capacity=20)
        for _ in range(10):
            buffer.add(make_sequence())
        batch = buffer.sample(batch_size=4)
        assert batch.shape == (4, *SEQ_SHAPE)

    def test_sample_empty_buffer_returns_empty_array(self) -> None:
        batch = ReplayBuffer(capacity=10).sample(batch_size=4)
        assert batch.shape[0] == 0

    def test_sample_prioritized_empty_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            ReplayBuffer(capacity=10).sample_prioritized(batch_size=4)

    def test_sample_prioritized_shapes_and_weight_bounds(self) -> None:
        buffer = ReplayBuffer(capacity=20)
        for i in range(10):
            buffer.add(make_sequence(float(i)), loss=float(i))
        batch, weights = buffer.sample_prioritized(batch_size=6)
        assert batch.shape == (6, *SEQ_SHAPE)
        assert weights.shape == (6,)
        assert np.all(weights > 0.0)
        assert np.all(weights <= 1.0 + 1e-9)

    def test_sample_prioritized_favors_high_loss(self) -> None:
        buffer = ReplayBuffer(capacity=50)
        for _ in range(20):
            buffer.add(make_sequence(0.0), loss=0.0)
        buffer.add(make_sequence(99.0), loss=1000.0)
        batch, _ = buffer.sample_prioritized(batch_size=40, alpha=1.0)
        # With alpha=1, the high-loss sequence has ~1.0 sampling probability.
        high_loss_count = int(np.sum(batch[:, 0, 0] == 99.0))
        assert high_loss_count > 30

    def test_sample_prioritized_alpha_zero_uniform_weights(self) -> None:
        buffer = ReplayBuffer(capacity=20)
        for i in range(8):
            buffer.add(make_sequence(), loss=float(i))
        _, weights = buffer.sample_prioritized(batch_size=10, alpha=0.0)
        # Uniform priorities -> all importance weights are exactly 1.
        assert np.allclose(weights, 1.0)

    def test_sample_by_label_returns_only_matching(self) -> None:
        buffer = ReplayBuffer(capacity=20)
        buffer.add(make_sequence(1.0), label="anomaly")
        buffer.add(make_sequence(2.0), label="normal")
        buffer.add(make_sequence(3.0), label="anomaly")
        batch = buffer.sample_by_label("anomaly", batch_size=10)
        assert batch.shape == (2, *SEQ_SHAPE)
        assert set(batch[:, 0, 0].tolist()) == {1.0, 3.0}

    def test_sample_by_label_caps_at_batch_size(self) -> None:
        buffer = ReplayBuffer(capacity=20)
        for _ in range(6):
            buffer.add(make_sequence(), label="normal")
        batch = buffer.sample_by_label("normal", batch_size=2)
        assert batch.shape == (2, *SEQ_SHAPE)

    def test_sample_by_label_missing_label_raises(self) -> None:
        buffer = ReplayBuffer(capacity=10)
        buffer.add(make_sequence(), label="normal")
        with pytest.raises(ValueError, match="no_such_label"):
            buffer.sample_by_label("no_such_label")


# ---------------------------------------------------------------------------
# ReplayBuffer — statistics & clear
# ---------------------------------------------------------------------------


class TestReplayBufferStatistics:
    def test_statistics_empty_buffer(self) -> None:
        stats = ReplayBuffer(capacity=10).get_statistics()
        assert stats == {"size": 0, "warning": "Empty buffer"}

    def test_statistics_populated_buffer(self) -> None:
        buffer = ReplayBuffer(capacity=4)
        buffer.add(make_sequence(), loss=1.0, label="normal")
        buffer.add(make_sequence(), loss=3.0, label="anomaly")
        stats = buffer.get_statistics()
        assert stats["size"] == 2
        assert stats["capacity"] == 4
        assert stats["fill_ratio"] == pytest.approx(0.5)
        assert stats["loss_stats"]["min"] == pytest.approx(1.0)
        assert stats["loss_stats"]["max"] == pytest.approx(3.0)
        assert stats["loss_stats"]["mean"] == pytest.approx(2.0)
        assert stats["loss_stats"]["std"] == pytest.approx(1.0)
        assert stats["label_distribution"] == {"normal": 1, "anomaly": 1}

    def test_clear_resets_buffer_and_warning_flag(self) -> None:
        buffer = ReplayBuffer(capacity=2)
        buffer.add(make_sequence(), loss=0.5, label="x")
        buffer.add(make_sequence())
        assert buffer._capacity_warned is True
        buffer.clear()
        assert len(buffer) == 0
        assert len(buffer.losses) == 0
        assert len(buffer.timestamps) == 0
        assert len(buffer.labels) == 0
        assert buffer._capacity_warned is False


# ---------------------------------------------------------------------------
# AnomalyBuffer — tri-state labels
# ---------------------------------------------------------------------------


class TestAnomalyBuffer:
    def test_unconfirmed_anomaly_labeled_suspected(self) -> None:
        buffer = AnomalyBuffer(capacity=10)
        buffer.add_anomaly(make_sequence(), anomaly_score=0.9)
        assert buffer.labels[0] == "suspected_anomaly"

    def test_confirmed_anomaly_label(self) -> None:
        buffer = AnomalyBuffer(capacity=10)
        buffer.add_anomaly(make_sequence(), anomaly_score=0.9, is_true_anomaly=True)
        assert buffer.labels[0] == "confirmed_anomaly"

    def test_false_positive_label(self) -> None:
        buffer = AnomalyBuffer(capacity=10)
        buffer.add_anomaly(make_sequence(), anomaly_score=0.9, is_true_anomaly=False)
        assert buffer.labels[0] == "false_positive"

    def test_anomaly_score_used_as_loss_and_tracked(self) -> None:
        buffer = AnomalyBuffer(capacity=10)
        buffer.add_anomaly(make_sequence(), anomaly_score=0.77)
        assert buffer.losses[0] == pytest.approx(0.77)
        assert list(buffer.anomaly_scores) == [pytest.approx(0.77)]
        assert len(buffer) == 1

    def test_inherited_add_keeps_scores_aligned(self) -> None:
        """P4 desync guard: mixing plain add() with add_anomaly() must keep
        anomaly_scores index-aligned with the sequence deque."""
        buffer = AnomalyBuffer(capacity=10)
        buffer.add(make_sequence(), loss=0.1)  # inherited add, no aux append pre-fix
        buffer.add_anomaly(make_sequence(), anomaly_score=0.9)
        assert len(buffer.anomaly_scores) == len(buffer)
        assert list(buffer.anomaly_scores) == [pytest.approx(0.1), pytest.approx(0.9)]


# ---------------------------------------------------------------------------
# SeasonalBuffer
# ---------------------------------------------------------------------------


class TestSeasonalBuffer:
    def test_add_seasonal_tracks_season(self) -> None:
        buffer = SeasonalBuffer(capacity=10)
        buffer.add_seasonal(make_sequence(), season="spring", loss=0.1)
        assert list(buffer.seasons) == ["spring"]
        assert buffer.losses[0] == pytest.approx(0.1)

    def test_sample_season_returns_matching_only(self) -> None:
        buffer = SeasonalBuffer(capacity=10)
        buffer.add_seasonal(make_sequence(1.0), season="spring")
        buffer.add_seasonal(make_sequence(2.0), season="summer")
        buffer.add_seasonal(make_sequence(3.0), season="spring")
        batch = buffer.sample_season("spring", batch_size=10)
        assert batch is not None
        assert batch.shape == (2, *SEQ_SHAPE)
        assert set(batch[:, 0, 0].tolist()) == {1.0, 3.0}

    def test_sample_season_caps_at_batch_size(self) -> None:
        buffer = SeasonalBuffer(capacity=10)
        for _ in range(5):
            buffer.add_seasonal(make_sequence(), season="winter")
        batch = buffer.sample_season("winter", batch_size=3)
        assert batch is not None
        assert batch.shape == (3, *SEQ_SHAPE)

    def test_sample_season_no_match_returns_none(self) -> None:
        buffer = SeasonalBuffer(capacity=10)
        buffer.add_seasonal(make_sequence(), season="summer")
        assert buffer.sample_season("autumn") is None

    def test_inherited_add_does_not_misalign_seasons(self) -> None:
        """P4 desync guard: a plain add() between seasonal adds must not shift
        season indices, or sample_season would return the wrong sequences."""
        buffer = SeasonalBuffer(capacity=10)
        buffer.add_seasonal(make_sequence(1.0), season="spring")
        buffer.add(make_sequence(2.0))  # untagged — must not steal 'spring'
        buffer.add_seasonal(make_sequence(3.0), season="spring")
        assert len(buffer.seasons) == len(buffer)
        batch = buffer.sample_season("spring", batch_size=10)
        assert batch is not None
        # Only the two genuinely-spring sequences, never the untagged one.
        assert set(batch[:, 0, 0].tolist()) == {1.0, 3.0}

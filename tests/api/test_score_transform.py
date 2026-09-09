"""Tests for the canonical anomaly score transform — ADR-005 §2.1.

Acceptance criteria: the seven-row worked example from the ADR must pass
exactly.  Monotonicity, boundedness, and non-finite guard are also covered.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import pytest

from src.api.services.score_transform import canonical


# ---------------------------------------------------------------------------
# ADR-005 §2.1 — Seven-row worked example (exact acceptance criteria)
# ---------------------------------------------------------------------------

class TestSevenRowWorkedExample:
    """The exact numbers from the ADR table are the acceptance criteria."""

    def test_live_noise_floor(self):
        """Live raw 0.017 → canonical 0.17 (below cutoff)."""
        result = canonical(0.017, 0.05, None)
        assert result is not None
        assert abs(result - 0.17) < 1e-9, f"Expected 0.17, got {result}"

    def test_live_at_threshold(self):
        """Live raw 0.05 → canonical 0.50 (exactly at cutoff)."""
        result = canonical(0.05, 0.05, None)
        assert result is not None
        assert abs(result - 0.50) < 1e-9, f"Expected 0.50, got {result}"

    def test_live_compound_episode(self):
        """Live raw 0.08 → canonical 0.69 (ADR table row, above cutoff)."""
        # 0.5 + 0.5 * (0.08 - 0.05) / 0.08 = 0.5 + 0.5 * 0.375 = 0.6875
        result = canonical(0.08, 0.05, None)
        assert result is not None
        assert abs(result - 0.6875) < 1e-4, f"Expected ~0.6875, got {result}"

    def test_live_demo_peak(self):
        """Live raw 0.36 → canonical 0.93 (demo peak)."""
        # 0.5 + 0.5 * (0.36 - 0.05) / 0.36 = 0.5 + 0.5 * 0.8611 ≈ 0.9306
        result = canonical(0.36, 0.05, None)
        assert result is not None
        assert abs(result - 0.9306) < 1e-4, f"Expected ~0.9306, got {result}"

    def test_batch_below_threshold(self):
        """Batch raw 0.28 → canonical 0.175 (below cutoff)."""
        result = canonical(0.28, 0.8, 1.0)
        assert result is not None
        assert abs(result - 0.175) < 1e-9, f"Expected 0.175, got {result}"

    def test_batch_at_threshold(self):
        """Batch raw 0.8 → canonical 0.50 (exactly at cutoff)."""
        result = canonical(0.8, 0.8, 1.0)
        assert result is not None
        assert abs(result - 0.50) < 1e-9, f"Expected 0.50, got {result}"

    def test_batch_strong_anomaly(self):
        """Batch raw 0.99 → canonical 0.97 (strong anomaly)."""
        # 0.5 + 0.5 * (0.99 - 0.8) / (1.0 - 0.8) = 0.5 + 0.5 * 0.95 = 0.975
        result = canonical(0.99, 0.8, 1.0)
        assert result is not None
        assert abs(result - 0.975) < 1e-9, f"Expected 0.975, got {result}"


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------

class TestBoundaryConditions:

    def test_raw_zero_maps_to_zero(self):
        assert canonical(0.0, 0.05, None) == 0.0

    def test_raw_negative_clamped_to_zero(self):
        assert canonical(-0.001, 0.05, None) == 0.0
        assert canonical(-100.0, 0.8, 1.0) == 0.0

    def test_bounded_raw_at_hi_maps_to_one(self):
        result = canonical(1.0, 0.8, 1.0)
        assert result is not None
        assert abs(result - 1.0) < 1e-9

    def test_bounded_raw_above_hi_can_exceed_one_when_hi_exceeds_raw(self):
        # raw = 1.5, p = 0.8, hi = 1.0  →  0.5 + 0.5 * 0.7 / 0.2 = 0.5 + 1.75 = 2.25
        # Caller should clamp if they care, but the transform is honest.
        result = canonical(1.5, 0.8, 1.0)
        assert result is not None
        assert result > 1.0  # explicitly out of [0,1]; caller's responsibility

    def test_unbounded_asymptotes_below_one(self):
        # As raw → ∞, canonical → 0.5 + 0.5 = 1.0 but never reaches it.
        for raw in (10.0, 100.0, 1_000_000.0):
            result = canonical(raw, 0.05, None)
            assert result is not None
            assert result < 1.0, f"Expected < 1.0 for raw={raw}, got {result}"
            assert result > 0.9, f"Expected > 0.9 for raw={raw}, got {result}"


# ---------------------------------------------------------------------------
# Monotonicity and [0, 1] bounds across both branches
# ---------------------------------------------------------------------------

class TestMonotonicity:

    def _check_monotone(
        self,
        p: float,
        hi: Optional[float],
        values: list[float],
    ) -> None:
        scores = [canonical(v, p, hi) for v in values]
        for i in range(1, len(scores)):
            prev, curr = scores[i - 1], scores[i]
            if prev is None or curr is None:
                continue
            assert curr >= prev, (
                f"Monotonicity violated: canonical({values[i-1]}) = {prev} "
                f"> canonical({values[i]}) = {curr}  (p={p}, hi={hi})"
            )

    def _check_bounded(
        self,
        p: float,
        hi: Optional[float],
        values: list[float],
    ) -> None:
        for v in values:
            result = canonical(v, p, hi)
            if result is None:
                continue
            if v >= 0.0:
                # Only check [0,1] for bounded branch (hi not None) or reasonable raws
                if hi is not None:
                    assert 0.0 <= result <= 1.0 + 1e-9, (
                        f"Out of [0,1]: canonical({v}, {p}, {hi}) = {result}"
                    )

    def test_live_branch_monotone(self):
        values = [i * 0.01 for i in range(0, 40)]  # 0.00 to 0.39
        self._check_monotone(0.05, None, values)

    def test_batch_branch_monotone(self):
        values = [i * 0.05 for i in range(0, 21)]  # 0.00 to 1.00
        self._check_monotone(0.8, 1.0, values)

    def test_live_branch_bounded(self):
        # Unbounded branch won't exceed 1.0 for reasonable raw values
        for raw in [0.0, 0.01, 0.05, 0.10, 0.36, 1.0, 10.0]:
            result = canonical(raw, 0.05, None)
            if result is not None and raw >= 0.0:
                assert result >= 0.0, f"Negative canonical for raw={raw}"
                assert result < 1.0 + 1e-9, f"Exceeds 1.0 for raw={raw}"

    def test_batch_branch_bounded(self):
        values = [i * 0.05 for i in range(0, 21)]
        self._check_bounded(0.8, 1.0, values)

    def test_well_past_hi(self):
        """raw well past hi should still be strictly monotone from previous point."""
        p, hi = 0.8, 1.0
        scores = [canonical(r, p, hi) for r in [0.79, 0.80, 0.95, 1.00, 1.50, 2.00]]
        valids = [s for s in scores if s is not None]
        for i in range(1, len(valids)):
            assert valids[i] >= valids[i - 1], "Monotonicity failed past hi"


# ---------------------------------------------------------------------------
# Non-finite guard (ADR §2.4)
# ---------------------------------------------------------------------------

class TestNonFiniteGuard:

    def test_nan_raw_returns_none(self):
        assert canonical(float("nan"), 0.05, None) is None

    def test_pos_inf_raw_returns_none(self):
        assert canonical(float("inf"), 0.05, None) is None

    def test_neg_inf_raw_returns_none(self):
        assert canonical(float("-inf"), 0.05, None) is None

    def test_nan_p_returns_none(self):
        assert canonical(0.1, float("nan"), None) is None

    def test_inf_p_returns_none(self):
        assert canonical(0.1, float("inf"), None) is None

    def test_p_zero_returns_none(self):
        assert canonical(0.1, 0.0, None) is None

    def test_p_negative_returns_none(self):
        assert canonical(0.1, -0.05, None) is None

    def test_nan_hi_returns_none(self):
        assert canonical(0.5, 0.8, float("nan")) is None

    def test_inf_hi_returns_none(self):
        assert canonical(0.5, 0.8, float("inf")) is None

    def test_none_hi_is_valid_for_unbounded(self):
        """hi=None is the explicit unbounded sentinel, not a non-finite value."""
        result = canonical(0.05, 0.05, None)
        assert result is not None
        assert abs(result - 0.5) < 1e-9

    def test_hi_equals_p_returns_none(self):
        """hi == p is a degenerate calibration (zero denominator in branch 3); must return None."""
        # raw > p so branch 3 would fire — but hi==p is invalid calibration.
        result = canonical(0.9, 0.8, 0.8)
        assert result is None, f"Expected None for hi==p, got {result!r}"

    def test_hi_less_than_p_returns_none(self):
        """hi < p is an inverted calibration (negative denominator in branch 3); must return None."""
        result = canonical(0.9, 0.8, 0.5)
        assert result is None, f"Expected None for hi<p, got {result!r}"

    def test_hi_equals_p_logs_at_warning(self, caplog):
        """hi == p guard logs at WARNING with context fields (same pattern as other guards)."""
        with caplog.at_level(logging.WARNING, logger="src.api.services.score_transform"):
            result = canonical(
                0.9, 0.8, 0.8,
                tenant_id="t2",
                greenhouse_id="gh2",
                timestamp="2026-01-02T00:00:00Z",
            )
        assert result is None
        assert any("t2" in r.message or "t2" in str(r.args) for r in caplog.records), (
            "Expected tenant_id 't2' in log output for hi==p guard"
        )

    def test_nonfinite_logs_at_warning(self, caplog):
        """Non-finite inputs log at WARNING with context fields."""
        with caplog.at_level(logging.WARNING, logger="src.api.services.score_transform"):
            result = canonical(
                float("nan"),
                0.05,
                None,
                tenant_id="t1",
                greenhouse_id="g1",
                timestamp="2026-01-01T00:00:00Z",
            )
        assert result is None
        assert any("t1" in r.message or "t1" in str(r.args) for r in caplog.records), (
            "Expected tenant_id 't1' in log output"
        )


# ---------------------------------------------------------------------------
# Migration idempotency — the transform must be deterministic
# ---------------------------------------------------------------------------

class TestDeterminism:

    def test_same_inputs_always_give_same_output(self):
        for raw, p, hi in [
            (0.017, 0.05, None),
            (0.05, 0.05, None),
            (0.28, 0.8, 1.0),
            (0.8, 0.8, 1.0),
            (0.99, 0.8, 1.0),
        ]:
            r1 = canonical(raw, p, hi)
            r2 = canonical(raw, p, hi)
            assert r1 == r2, f"canonical({raw}, {p}, {hi}) is not deterministic"

    def test_applying_twice_is_different_from_once(self):
        """Canonical is NOT idempotent — applying twice gives a wrong answer.

        This test documents the expected behavior so implementors don't
        accidentally apply the transform twice.  The migration uses score_scale
        IS NULL as the idempotency key to prevent this.
        """
        p, hi = 0.05, None
        raw = 0.017
        once = canonical(raw, p, hi)
        assert once is not None
        # Applying canonical to an already-canonical score would give a
        # different (wrong) result.
        twice = canonical(once, p, hi)
        assert twice is not None
        assert abs(twice - raw) > 1e-9, (
            "Applying canonical twice accidentally equals raw — "
            "the double-transform bug would go undetected"
        )
        # More specifically: the double result must differ from once.
        assert abs(twice - once) > 1e-6, (
            "Applying canonical twice must give a different value than once"
        )

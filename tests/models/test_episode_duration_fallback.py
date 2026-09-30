"""Stuck-fault episode duration when no completed FaultWindow matches.

``episode_grouper._stuck_fault_duration`` has two strategies: a completed
``FaultWindow`` gives the full frozen span (``is_ongoing=False``), and
otherwise the freeze was still open at detection time, so the alert's own
gate-crossing duration is used as a lower bound (``is_ongoing=True``).

This file pins the second, fallback strategy. It is exercised through the
public ``group_alerts_into_episodes`` rather than the private helper, so the
pins survive a refactor of the helper's name or signature — every case here
passes ``completed_faults=None`` so no window can match.

The field the fallback reads changed: stuck hits now carry their hours in
``frozen_hours_at_detection`` and leave ``z_score`` as ``None``. Rows already
persisted in a ``Job.result``/``JobPortion.result`` blob still carry the
duration in the numeric ``z_score`` field and there is no backfill migration,
so both shapes must keep working — that legacy shape is pinned deliberately
here and is the one place in ``tests/`` that still constructs it.
"""

from __future__ import annotations

import pytest

from src.api.schemas.sensor_data import UploadHighAnomalyDetail
from src.config import STUCK_WALLCLOCK_MIN_HOURS
from src.models.episode_grouper import group_alerts_into_episodes
from src.models.stuck_fault_detector import (
    StuckCarry,
    WallclockStuckDetector,
)

SECONDS_PER_HOUR = 3600.0
FROZEN_CHANNEL = "humidity"
FROZEN_VALUE = 100.0
ONSET_TS = "2026-06-01T00:00:00+00:00"

# A z_score high enough that reading it instead of the new field would be
# unmistakable in the assertion (99 h vs 6 h), not a near-miss.
DECOY_Z_SCORE = 99.0


def _stuck_alert(**overrides: object) -> UploadHighAnomalyDetail:
    """A single stuck_fault hit; overrides set the duration-carrying fields."""
    fields: dict = {
        "score": 1.0,
        "timestamp": ONSET_TS,
        "detector": "stuck_fault",
        "column": FROZEN_CHANNEL,
        "severity": "urgent",
        "method": "wallclock_stuck",
        "value": FROZEN_VALUE,
    }
    fields.update(overrides)
    return UploadHighAnomalyDetail(**fields)


def _sole_episode(alert: UploadHighAnomalyDetail):
    """Group one alert with no completed faults, forcing the fallback path."""
    episodes = group_alerts_into_episodes([alert], completed_faults=None)
    assert len(episodes) == 1, f"Expected 1 episode, got {len(episodes)}"
    return episodes[0]


# ---------------------------------------------------------------------------
# Item 4 — the duration fallback, all four field combinations
# ---------------------------------------------------------------------------

GATE_SECONDS = STUCK_WALLCLOCK_MIN_HOURS * SECONDS_PER_HOUR

DURATION_FALLBACK_CASES = [
    pytest.param(
        {"z_score": STUCK_WALLCLOCK_MIN_HOURS},
        GATE_SECONDS,
        id="legacy-z_score-only",
    ),
    pytest.param(
        {"frozen_hours_at_detection": STUCK_WALLCLOCK_MIN_HOURS, "z_score": None},
        GATE_SECONDS,
        id="current-frozen_hours-only",
    ),
    pytest.param(
        {
            "frozen_hours_at_detection": STUCK_WALLCLOCK_MIN_HOURS,
            "z_score": DECOY_Z_SCORE,
        },
        GATE_SECONDS,
        id="both-present-new-field-wins",
    ),
    pytest.param({}, 0.0, id="neither-present"),
]


@pytest.mark.parametrize("duration_fields,expected_seconds", DURATION_FALLBACK_CASES)
def test_duration_fallback_reads_hours_from_either_shape(
    duration_fields: dict, expected_seconds: float,
) -> None:
    """``frozen_hours_at_detection`` wins; ``z_score`` is the legacy fallback.

    The ``both-present`` case is the one that pins precedence: a legacy blob
    re-read after the field was added could carry both, and reading the stale
    ``z_score`` would report 99 h instead of 6 h.
    """
    episode = _sole_episode(_stuck_alert(**duration_fields))
    assert episode.duration_seconds == expected_seconds


@pytest.mark.parametrize("duration_fields,_expected", DURATION_FALLBACK_CASES)
def test_duration_fallback_always_marks_the_episode_ongoing(
    duration_fields: dict, _expected: float,
) -> None:
    """No matching FaultWindow means the freeze was still open at detection.

    Holds even in the ``neither-present`` case, where the duration is a
    placeholder ``0.0`` rather than a measurement — ``is_ongoing`` reflects
    the absence of a completed window, not whether an hours value was found.
    """
    assert _sole_episode(_stuck_alert(**duration_fields)).is_ongoing is True


def test_legacy_shape_is_the_only_retained_z_score_stuck_fixture() -> None:
    """Documents why a numeric ``z_score`` on a stuck hit is still supported.

    Kept deliberately as the legacy-payload fixture: ``JobPortion.result``
    blobs written before ``frozen_hours_at_detection`` existed still hold
    stuck rows this way, and ``upload_consumer.py`` reconstructs them with
    ``UploadHighAnomalyDetail(**h)``. The read is only safe because the
    caller has already established ``detector == "stuck_fault"`` — a
    statistical row also has a numeric ``z_score``, so discriminating on
    whether ``z_score`` is present instead of on the detector would misread
    a z-score as a duration.
    """
    legacy = _stuck_alert(z_score=STUCK_WALLCLOCK_MIN_HOURS)
    assert legacy.frozen_hours_at_detection is None
    assert _sole_episode(legacy).duration_seconds == GATE_SECONDS


# ---------------------------------------------------------------------------
# Item 5 — a freeze still open at end of stream
# ---------------------------------------------------------------------------

def test_freeze_open_at_end_of_stream_gets_a_nonzero_duration() -> None:
    """A never-released freeze still yields duration == hours at the gate.

    Runs the real detector over a constant channel that never recovers, so
    ``carry.completed_faults`` stays empty and the fallback is the only
    source of a duration. Before the fallback read
    ``frozen_hours_at_detection`` this collapsed to ``0.0``, since ``z_score``
    had already become ``None`` — a still-open freeze reported as
    zero-duration.

    The duration is the gate-crossing value, not the full span of the input
    (~16.6 h here): the alert is stamped once, at the first gate crossing,
    and never revised.
    """
    import pandas as pd
    from datetime import datetime, timedelta, timezone

    n_rows = 200  # 199 * 300 s ≈ 16.6 h, comfortably past the 6 h gate
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    frame = pd.DataFrame({
        "timestamp": [start + timedelta(seconds=i * 300) for i in range(n_rows)],
        "temperature": [20.0 + 0.1 * (i % 50) for i in range(n_rows)],
        FROZEN_CHANNEL: [FROZEN_VALUE] * n_rows,
        "co2": [400.0 + (i % 20) for i in range(n_rows)],
        "light": [0.0] * n_rows,
    })

    detector = WallclockStuckDetector()
    carry = StuckCarry()
    raw_alerts = detector.detect_chunk(frame, carry)
    raw_alerts.extend(detector.flush(carry))

    assert len(raw_alerts) == 1, f"Expected 1 stuck alert, got {len(raw_alerts)}"
    assert carry.completed_faults == [], (
        "The freeze must still be open for this to pin the fallback path"
    )

    hours_at_gate = raw_alerts[0]["duration_hours"]
    alert = _stuck_alert(
        timestamp=raw_alerts[0]["timestamp"],
        z_score=None,
        frozen_hours_at_detection=hours_at_gate,
    )
    episodes = group_alerts_into_episodes(
        [alert], completed_faults=carry.completed_faults,
    )

    assert len(episodes) == 1
    assert episodes[0].duration_seconds > 0.0
    assert episodes[0].duration_seconds == hours_at_gate * SECONDS_PER_HOUR
    assert episodes[0].is_ongoing is True


# ---------------------------------------------------------------------------
# Item 6 — the 0.0 edge
# ---------------------------------------------------------------------------

def test_zero_frozen_hours_is_indistinguishable_from_no_duration() -> None:
    """``frozen_hours_at_detection=0.0`` falls through to the 0.0 last resort.

    The fallback's guard is ``duration_hours > 0``, so a genuine 0.0 and "no
    duration recoverable from this alert" produce the same
    ``(0.0, is_ongoing=True)`` result — the value is ambiguous by design and
    the episode cannot tell the two apart.

    Inherited behaviour, not introduced by the ``z_score`` →
    ``frozen_hours_at_detection`` split: the ``> 0`` guard predates it and was
    preserved deliberately. Currently unreachable in production, because the
    gate will not emit an alert below ``STUCK_WALLCLOCK_MIN_HOURS`` (6 h), so
    no real alert can carry 0.0 hours. Pinned so that if the gate ever drops
    to zero, or a hand-built payload reaches the grouper, the ambiguity is a
    documented decision rather than a surprise.
    """
    explicit_zero = _sole_episode(
        _stuck_alert(frozen_hours_at_detection=0.0, z_score=None)
    )
    nothing_at_all = _sole_episode(_stuck_alert())

    assert explicit_zero.duration_seconds == 0.0
    assert explicit_zero.duration_seconds == nothing_at_all.duration_seconds
    assert explicit_zero.is_ongoing == nothing_at_all.is_ongoing is True


def test_negative_frozen_hours_also_falls_through_to_zero() -> None:
    """The same ``> 0`` guard rejects a negative duration.

    Not reachable either — ``_check_gate`` computes ``last_dt - onset_dt`` on
    a monotonically advancing freeze — but it pins that the guard is
    ``> 0`` and not ``is not None``, which is what makes the 0.0 case above
    ambiguous in the first place.
    """
    episode = _sole_episode(
        _stuck_alert(frozen_hours_at_detection=-1.0, z_score=None)
    )
    assert episode.duration_seconds == 0.0
    assert episode.is_ongoing is True

"""JSON-persistence safety for tracker state written to LearningState.state_json.

These assert against ``json.dumps(..., allow_nan=False)`` rather than plain
``json.dumps``. Plain dumps emits the non-standard ``Infinity``/``NaN`` tokens,
which SQLite stores happily — so a permissive assertion passes locally while
PostgreSQL rejects the same payload in production with
``invalid input syntax for type json``. ``allow_nan=False`` is the strict
JSON-spec behaviour that matches what Postgres will accept.
"""

import json
import math

import pytest

from src.api.services.comprehensive_pipeline import _json_safe
from src.models.ewma_tracker import EWMATracker
from src.models.pot_threshold import POTThresholdFitter


def dumps_strict(payload: object) -> str:
    """Serialize the way PostgreSQL's json column will accept, or raise."""
    return json.dumps(payload, allow_nan=False)


class TestPOTThresholdSerialisation:
    def test_fresh_fitter_state_is_postgres_safe(self):
        """Regression: a brand-new fitter broke every learning-state write.

        _threshold starts at inf and only becomes finite after 50 observations
        with a 20-deep buffer. Serialising inf made the save fail, so the state
        never persisted, so the fitter was rebuilt at inf next run — warm-up
        could never end on Postgres.
        """
        dumps_strict(POTThresholdFitter().to_dict())

    def test_warm_up_threshold_serialises_as_null(self):
        assert POTThresholdFitter().to_dict()["threshold"] is None

    def test_null_threshold_round_trips_back_to_infinity(self):
        """None means "not fitted yet" — it must not load back as 0.0."""
        restored = POTThresholdFitter.from_dict(POTThresholdFitter().to_dict())
        assert restored.get_threshold() == math.inf

    def test_fitted_threshold_survives_the_round_trip(self):
        fitter = POTThresholdFitter()
        for value in range(100):
            fitter.update(float(value))
        fitted = fitter.to_dict()["threshold"]
        assert fitted is not None and math.isfinite(fitted)

        restored = POTThresholdFitter.from_dict(fitter.to_dict())
        assert restored.to_dict()["threshold"] == pytest.approx(fitted)

    def test_ewma_state_is_postgres_safe(self):
        tracker = EWMATracker()
        for value in range(20):
            tracker.update(value / 20.0)
        dumps_strict(tracker.to_dict())


class TestJsonSafe:
    """The write-boundary net behind the per-class fixes."""

    def test_non_finite_floats_become_null(self):
        payload = {"a": math.inf, "b": -math.inf, "c": math.nan, "d": 1.5}
        assert _json_safe(payload) == {"a": None, "b": None, "c": None, "d": 1.5}

    def test_nested_structures_are_walked(self):
        payload = {"outer": {"inner": [1.0, math.inf, {"deep": math.nan}]}}
        assert _json_safe(payload) == {"outer": {"inner": [1.0, None, {"deep": None}]}}
        dumps_strict(_json_safe(payload))

    def test_finite_values_and_non_floats_are_untouched(self):
        payload = {"n": 3, "s": "text", "b": True, "none": None, "f": 0.25}
        assert _json_safe(payload) == payload

    def test_tuples_become_lists(self):
        """JSON has no tuple; the column would store an array either way."""
        assert _json_safe({"t": (1.0, 2.0)}) == {"t": [1.0, 2.0]}

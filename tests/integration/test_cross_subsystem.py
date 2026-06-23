"""Integration tests: inference → online learner wiring (S2-2)."""

import inspect
import logging

import numpy as np
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Phase A: comprehensive_pipeline._observe_online_learner
# ---------------------------------------------------------------------------


def test_observe_online_learner_calls_manager_observe():
    """Online learner manager.observe() is called when the helper is invoked."""
    from src.api.services.comprehensive_pipeline import _observe_online_learner

    mock_manager = MagicMock()

    # The helper imports _get_manager from src.api.routes.learning at call time.
    with patch(
        "src.api.routes.learning._get_manager", return_value=mock_manager
    ):
        seq = np.zeros((20, 4), dtype=np.float32)
        _observe_online_learner(
            customer_id="tenant_test",
            sequence=seq,
            anomaly_score=0.9,
        )

    # Manager.observe() must be called with (tenant_id, sequence).
    mock_manager.observe.assert_called_once_with("tenant_test", seq)


def test_observe_online_learner_guard_in_pipeline_source():
    """The is_anomaly guard and _observe_online_learner call exist in pipeline run()."""
    from src.api.services import comprehensive_pipeline

    source = inspect.getsource(comprehensive_pipeline)
    assert "if is_anomaly:" in source, "Missing is_anomaly guard in pipeline"
    assert "_observe_online_learner" in source, (
        "_observe_online_learner not referenced in pipeline"
    )


def test_observe_online_learner_exception_propagates():
    """If _get_manager raises, _observe_online_learner propagates the error.

    The caller (pipeline.run) wraps the call in try/except so the pipeline
    itself is non-fatal, but the helper should not silently swallow errors.
    """
    from src.api.services.comprehensive_pipeline import _observe_online_learner

    with patch(
        "src.api.routes.learning._get_manager",
        side_effect=RuntimeError("manager unavailable"),
    ):
        with pytest.raises(RuntimeError, match="manager unavailable"):
            _observe_online_learner(
                customer_id="t",
                sequence=np.zeros((1, 4), dtype=np.float32),
                anomaly_score=0.9,
            )


def test_pipeline_source_try_except_wraps_observe():
    """The pipeline wraps _observe_online_learner in try/except for safety."""
    from src.api.services import comprehensive_pipeline

    source = inspect.getsource(comprehensive_pipeline)
    # Both the guard and the non-critical exception handling must be present.
    assert "logger.exception" in source
    assert "non-critical" in source


# ---------------------------------------------------------------------------
# Phase B: feedback_integrated._observe_online_learner_from_feedback
# ---------------------------------------------------------------------------


def test_feedback_helper_importable():
    """_observe_online_learner_from_feedback is importable and callable."""
    from src.api.routes.feedback_integrated import (
        _observe_online_learner_from_feedback,
    )

    assert callable(_observe_online_learner_from_feedback)


def test_feedback_helper_uses_incident_sequence():
    """When the incident has a sequence, manager.observe() is called with it."""
    from src.api.routes.feedback_integrated import (
        _observe_online_learner_from_feedback,
    )

    mock_manager = MagicMock()
    seq = [[float(i)] * 4 for i in range(20)]
    incident = MagicMock()
    incident.sequence = seq
    incident.embedding = None
    incident.id = "inc-001"

    with patch(
        "src.api.routes.learning._get_manager", return_value=mock_manager
    ):
        _observe_online_learner_from_feedback(
            customer_id="tenant_fb",
            incident=incident,
            is_anomaly=True,
        )

    mock_manager.observe.assert_called_once()
    call_args = mock_manager.observe.call_args
    assert call_args[0][0] == "tenant_fb"
    called_seq = call_args[0][1]
    assert isinstance(called_seq, np.ndarray)
    assert called_seq.shape == (20, 4)


def test_feedback_helper_falls_back_to_embedding():
    """When sequence is None, the embedding is used as a proxy sequence."""
    from src.api.routes.feedback_integrated import (
        _observe_online_learner_from_feedback,
    )

    mock_manager = MagicMock()
    embedding = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    incident = MagicMock()
    incident.sequence = None
    incident.embedding = embedding
    incident.id = "inc-002"

    with patch(
        "src.api.routes.learning._get_manager", return_value=mock_manager
    ):
        _observe_online_learner_from_feedback(
            customer_id="tenant_fb",
            incident=incident,
            is_anomaly=False,
        )

    mock_manager.observe.assert_called_once()
    call_args = mock_manager.observe.call_args
    assert call_args[0][0] == "tenant_fb"
    called_seq = call_args[0][1]
    assert isinstance(called_seq, np.ndarray)
    assert called_seq.shape == (1, 8)


def test_feedback_helper_skips_when_no_data(caplog):
    """When neither sequence nor embedding is present, observe is not called."""
    from src.api.routes.feedback_integrated import (
        _observe_online_learner_from_feedback,
    )

    mock_manager = MagicMock()
    incident = MagicMock()
    incident.sequence = None
    incident.embedding = None
    incident.id = "inc-003"

    with patch(
        "src.api.routes.learning._get_manager", return_value=mock_manager
    ):
        with caplog.at_level(logging.DEBUG):
            _observe_online_learner_from_feedback(
                customer_id="tenant_fb",
                incident=incident,
                is_anomaly=True,
            )

    mock_manager.observe.assert_not_called()


# ---------------------------------------------------------------------------
# Phase C: observability logging on confirmed anomalies
# ---------------------------------------------------------------------------


def test_feedback_route_source_has_confirmed_anomaly_logging():
    """The feedback route logs when an incident is confirmed as an anomaly."""
    from src.api.routes import feedback_integrated

    source = inspect.getsource(feedback_integrated)
    assert "confirmed as anomaly" in source, (
        "Missing observability log for confirmed anomaly in feedback_integrated"
    )
    assert "suggest_fixes" in source, (
        "Missing reference to suggest_fixes in observability log"
    )


def test_feedback_route_source_wraps_observe_in_try_except():
    """The feedback route wraps online learner observe in try/except."""
    from src.api.routes import feedback_integrated

    source = inspect.getsource(feedback_integrated)
    assert "non-critical" in source, (
        "Missing non-critical exception annotation in feedback_integrated"
    )

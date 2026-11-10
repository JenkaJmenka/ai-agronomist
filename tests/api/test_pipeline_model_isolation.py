"""BACKLOG-19 regression: the pipeline must not mutate a model it does not own.

``ComprehensiveInferencePipeline._get_cached_components`` builds an
``AnomalyAttributor``, and ``AnomalyAttributor.__init__`` mutates whatever
model it is handed **in place** -- ``.to(device)`` then ``.eval()``
(``src/models/anomaly_attributor.py:79-81``, documented there as a side
effect). Until ``1b9a87d`` the attributor was built on ``self.detector.model``
directly, which is not the pipeline's model to mutate: ``DetectorResolver``
caches one ``AnomalyDetector`` instance per scope
(``src/live/detector_resolver.py:97``) and ``get_cached_detector`` is a
process-wide LRU (``src/api/services/detector_cache.py:49``), so the same
``LSTMVAE`` feeds the live scoring hot path and the sync ``/api/inference``
routes.

Why in-place mutation of a *shared* model is a bug and not merely untidy:
``Module.to()`` runs ``_apply`` unconditionally, even for a no-op same-device
move, and ``RNNBase._apply`` empties ``self._flat_weight_refs`` before
delegating to ``super()._apply(fn)``, restoring it only afterwards
(``torch/nn/modules/rnn.py:298``). A forward pass that reaches
``_weights_have_changed`` (``rnn.py:364``) inside that window zips the empty
ref list against ``_flat_weights_names`` under ``strict=True`` and raises
``ValueError: zip() argument 2 is longer than argument 1``.
``src/live/scoring.py:137`` logs and swallows it, so the reading persists with
``anomaly_score`` NULL -- a silent data loss with no error surfaced anywhere.

These tests pin the invariant that removes the race rather than the race
itself: the pipeline hands the attributor a private copy, so no interleaving
of pipeline and scoring work can corrupt the shared model. A test that tried
to lose the race instead would be reproducing a ~1.5-3% flake, which is
exactly what BACKLOG-19 removes from the suite.

The detector here is a real ``AnomalyDetector`` loaded from a checkpoint, not a
mock: the assertions below are about ``torch`` module state (training flag,
``_flat_weight_refs``), and against a ``MagicMock`` both ``.to()`` and
``.eval()`` are silent no-ops that would make every mode assertion pass
vacuously.
"""

from __future__ import annotations

import numpy as np
import pytest
import torch
import torch.nn as nn

from src.api.services.comprehensive_pipeline import (
    ComprehensiveInferencePipeline,
    build_default_baseline,
)
from src.config import HIDDEN_DIMS, MODEL_CONFIG
from src.models.inference import AnomalyDetector
from src.models.lstm_vae import LSTMVAE

# LSTMVAE builds one nn.LSTM per hidden dim in the encoder (src/models/lstm_vae.py:77)
# and one more per hidden dim in the decoder (:93).
EXPECTED_LSTM_COUNT = 2 * len(HIDDEN_DIMS)


@pytest.fixture
def shared_detector(tmp_path) -> AnomalyDetector:
    """A detector standing in for the one DetectorResolver caches and shares."""
    model = LSTMVAE(**MODEL_CONFIG)
    checkpoint_path = str(tmp_path / "backlog19_model.pt")
    torch.save(
        {"model_config": MODEL_CONFIG, "model_state_dict": model.state_dict()},
        checkpoint_path,
    )
    return AnomalyDetector(checkpoint_path=checkpoint_path)


@pytest.fixture
def attribution_components(shared_detector: AnomalyDetector):
    """Build the pipeline's components once, on a model left in training mode.

    ``AnomalyDetector._load_model`` already calls ``model.eval()``, so training
    mode has to be set deliberately for the mode flip to be observable. It is
    the cleanest single signal of the defect: pre-fix the attributor's
    ``.eval()`` landed on this very instance.

    The baseline is ``build_default_baseline()`` -- the array the production
    path itself passes when a caller supplies none (``_prepare_baseline``), and
    deterministic by construction since it is tiled sensor-range midpoints with
    no sampling.
    """
    pipeline = ComprehensiveInferencePipeline(detector=shared_detector)
    caller_model = shared_detector.model
    caller_model.train()

    baseline = build_default_baseline()
    _, attributor, _ = pipeline._get_cached_components(baseline)

    return caller_model, attributor


def _lstm_submodules(model: nn.Module) -> list[nn.LSTM]:
    return [module for module in model.modules() if isinstance(module, nn.LSTM)]


class TestPipelineDoesNotMutateSharedDetectorModel:
    """BACKLOG-19: _get_cached_components must leave detector.model untouched."""

    def test_attributor_does_not_share_the_detector_model_instance(
        self, attribution_components
    ) -> None:
        caller_model, attributor = attribution_components

        assert attributor.model is not caller_model

    def test_detector_model_is_left_in_the_training_mode_it_was_in(
        self, attribution_components
    ) -> None:
        caller_model, _ = attribution_components

        assert caller_model.training is True

    def test_attributor_model_is_switched_to_eval_mode(
        self, attribution_components
    ) -> None:
        """The copy must still get what attribution needs: dropout disabled."""
        _, attributor = attribution_components

        assert attributor.model.training is False

    def test_attributor_model_carries_the_detector_weights(
        self, attribution_components
    ) -> None:
        """A private copy, not a fresh model -- scoring must be unchanged."""
        caller_model, attributor = attribution_components

        caller_state = caller_model.state_dict()
        copy_state = attributor.model.state_dict()

        assert copy_state.keys() == caller_state.keys()
        for name, tensor in caller_state.items():
            assert torch.equal(copy_state[name], tensor), f"weights differ at {name}"

    def test_detector_lstm_weight_refs_survive_intact(
        self, attribution_components
    ) -> None:
        """No torn RNNBase._apply state left behind on the shared model.

        ``_weights_have_changed`` zips these two lists under ``strict=True``,
        so a length mismatch is precisely what raises mid-forward-pass.
        """
        caller_model, _ = attribution_components

        lstms = _lstm_submodules(caller_model)
        assert len(lstms) == EXPECTED_LSTM_COUNT

        for index, lstm in enumerate(lstms):
            refs = lstm._flat_weight_refs
            assert isinstance(refs, list), f"LSTM {index} ref list was replaced"
            assert len(refs) == len(lstm._flat_weights_names), (
                f"LSTM {index} has {len(refs)} weight refs, expected "
                f"{len(lstm._flat_weights_names)}"
            )

    def test_detector_model_still_scores_after_the_pipeline_ran(
        self, shared_detector: AnomalyDetector, attribution_components
    ) -> None:
        """The end the user sees: the shared model remains usable for scoring."""
        caller_model, _ = attribution_components
        caller_model.eval()

        sequences = build_default_baseline(num_sequences=2).astype(np.float32)
        scores = shared_detector.predict(sequences)

        assert np.isfinite(scores).all()

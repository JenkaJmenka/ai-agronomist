"""The missing-checkpoint contract, tested with nothing patched out.

This file lives in ``tests/models/`` deliberately. ``tests/api/conftest.py``
has an autouse fixture that patches ``AnomalyDetector._load_model`` and
substitutes a stub path for a ``None`` checkpoint — which is exactly what hid
this bug from 3785 tests while production returned 503 and the live feed scored
nothing. Nothing here is patched, so these assertions describe real behaviour.

Context: ``AnomalyDetector(None)`` used to glob ``models/checkpoints/epoch_*.pt``
(zero matches in every environment) and fall back to the literal
``models/baseline.pt`` — untracked, absent locally, and excluded from the image
by ``.dockerignore`` lines 24-26. Models are resolved from the DB artifact
store; there is no shared baseline.
"""

from __future__ import annotations

import os

import pytest

from src.models.inference import AnomalyDetector, MissingCheckpointError


class TestAnomalyDetectorRequiresACheckpoint:
    def test_none_checkpoint_raises(self) -> None:
        with pytest.raises(MissingCheckpointError):
            AnomalyDetector(checkpoint_path=None)

    def test_default_argument_also_raises(self) -> None:
        """The parameter defaults to None, so the no-arg call must fail too."""
        with pytest.raises(MissingCheckpointError):
            AnomalyDetector()

    def test_error_is_a_runtime_error(self) -> None:
        """Callers that already handle _load_model's RuntimeError keep working."""
        assert issubclass(MissingCheckpointError, RuntimeError)

    def test_message_points_at_the_real_resolution_path(self) -> None:
        with pytest.raises(MissingCheckpointError) as exc_info:
            AnomalyDetector(checkpoint_path=None)
        message = str(exc_info.value)
        assert "checkpoint_path" in message
        assert "artifact store" in message or "model_artifacts" in message

    def test_no_baseline_file_is_consulted(self) -> None:
        """The dead fallback must be gone, not merely unreachable.

        If either fallback path is ever reintroduced this fails, because
        neither exists in the repo, the image, or a dev checkout.
        """
        import src.models.inference as inference_module

        source = open(inference_module.__file__).read()
        assert "models/baseline.pt" not in source, (
            "the dead baseline literal is back — it resolves to no file in any "
            "environment and is excluded from the image by .dockerignore"
        )
        assert "epoch_*.pt" not in source, (
            "the epoch_*.pt glob is back — it has never matched in any "
            "environment; models/checkpoints/ holds the DB artifact cache"
        )
        assert not os.path.exists("models/baseline.pt"), (
            "a models/baseline.pt appeared in the working tree; this contract "
            "assumes none ships, per .dockerignore"
        )


class TestDetectorCacheDoesNotInventABaseline:
    def test_get_cached_detector_propagates_the_error_for_none(self) -> None:
        """No silent baseline substitution at the cache layer either.

        ``get_cached_detector(None)`` keys the LRU on ``"__baseline__"``; the
        KeyError that key produces is an ordinary cache miss, and the real
        failure must surface rather than being swallowed.
        """
        from src.api.services.detector_cache import (
            clear_detector_cache,
            get_cached_detector,
        )

        clear_detector_cache()
        try:
            with pytest.raises(MissingCheckpointError):
                get_cached_detector(None)
        finally:
            clear_detector_cache()


class TestTenantCheckpointResolution:
    @pytest.mark.asyncio
    async def test_no_active_model_raises_no_active_model_error(self) -> None:
        """A tenant with no deployment is an explicit error, not a None."""
        from unittest.mock import AsyncMock, patch

        from src.api.services.detector_cache import (
            NoActiveModelError,
            resolve_tenant_checkpoint,
        )

        with patch(
            "src.models.model_registry.ModelRegistry.get_active_model",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(NoActiveModelError) as exc_info:
                await resolve_tenant_checkpoint(object(), "tenant_without_a_model")

        assert "tenant_without_a_model" in str(exc_info.value)

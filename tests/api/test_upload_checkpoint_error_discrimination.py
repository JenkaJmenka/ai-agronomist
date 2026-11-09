"""``resolve_upload_checkpoint`` must tell two failures apart (BACKLOG-14 B2).

Cold-start training can fail in ways that need different answers for the
tenant, and two of them are distinguishable:

1. **``InsufficientDataError``** — too little clean data was supplied. The
   tenant should be told to upload a longer history.
2. **``ExcessivePass1DiscardError``** — too *much* was discarded by cleaning.
   The upload may have been plenty long; the problem is sensor quality. Telling
   this tenant to "upload a longer history" sends them to do the one thing that
   cannot help. This is the behaviour change B2 is about.

Path 2 is separable because ``ExcessivePass1DiscardError`` is a genuine
``ValueError`` **subclass**, so an ``except`` clause ahead of the generic one
can claim it.

## The third path, and why there is no test for it

The brief originally asked for a third: a bare ``ValueError`` surfacing as a
server error rather than as a statement about the tenant's data. **That is not
reachable, and this module deliberately does not assert it.**

``src/models/bootstrap.py:61`` is ``InsufficientDataError = ValueError`` — a
bare **alias**, not a subclass. Verified at runtime:
``InsufficientDataError is ValueError`` is ``True``. So a genuine
insufficient-clean-rows raise and an unrelated bare ``ValueError`` from
elsewhere in the same call chain are *the identical Python class with no
distinguishing attribute*, and no ``isinstance`` check anywhere can separate
them. There are five reachable bare ``raise ValueError`` sites in the chain,
including ``cold_start_training.py:73`` ("Cold-start training produced no
model") and ``:164`` ("Invalid or unreadable CSV file").

I also deliberately did **not** add a test pinning where a bare ``ValueError``
currently goes. Such a test would cement a known defect, which is the same
reason this stack does not pin current stuck-sensor behaviour. What
:class:`TestWhyTheThirdPathIsUnreachable` asserts instead is the *structural
cause* — the alias itself. That is honest, it is true today, and it is a
tripwire rather than a lock: it fails the moment someone converts the alias to
a subclass, which is exactly the moment the four catch sites that rely on
``ValueError`` mapping to HTTP 400 need re-auditing. Making that conversion
naively would turn legitimate client-data errors like "Invalid or unreadable
CSV file" into 500s, so it is a design decision for the user, not a test fix.

## Why this went uncovered

``tests/api/test_upload_checkpoint.py`` monkeypatches
``train_cold_start_from_csv`` away entirely and asserts only that *something*
is raised — which is true of every path.

**Expected-red.** ``TestExcessiveDiscardOnAutoTrain`` is written against the
intended post-fix behaviour and fails until backend-engineer's branch lands.
The fix is in ``src/api/services/``, not ours. Note path 2 on the **forced**
branch already passes — ``if train_model: raise`` re-raises untouched — so the
defect is specific to the auto-train fallback, and that asymmetry is pinned
below.

The tests assert **behaviour, not mechanism**: which ``except`` arrangement
backend chooses is its call.
"""

from __future__ import annotations

import pytest

from src.api.services import upload_checkpoint
from src.api.services.upload_checkpoint import (
    NoModelAvailableError,
    resolve_upload_checkpoint,
)
from src.models.bootstrap import InsufficientDataError
from src.models.model_registry import ModelRegistry
from src.models.pass1_discard_guard import ExcessivePass1DiscardError

TENANT = "tenant_x"
CSV_PATH = "/tmp/does-not-need-to-exist.csv"

INSUFFICIENT_DETAIL = (
    "Insufficient clean rows: 42 (minimum 100 required after Pass 1 filtering)"
)

# The tenant-safe discard message, as `_with_warning` builds it and
# `ColdStartBootstrap.run` raises it.
DISCARD_DETAIL = (
    "Data cleaning removed 53004 of 104921 readings (50.5%), above the 25% "
    "limit, so training was stopped. Please check the sensors flagged above "
    "and upload again."
)

# Prose that belongs ONLY to the too-little-data answer. Its appearance on any
# other path is the relabelling bug.
SHORTAGE_PHRASES = ("not enough clean data", "longer history")


@pytest.fixture(autouse=True)
def tenant_has_no_active_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the cold-start branch: no active model means training is attempted."""

    async def _no_active_model(self, tenant_id: str):
        return None

    monkeypatch.setattr(ModelRegistry, "get_active_model", _no_active_model)


def _trainer_raising(exc: Exception):
    async def _train(db, tenant_id, csv_path):
        raise exc

    return _train


@pytest.fixture
def failing_trainer(monkeypatch: pytest.MonkeyPatch):
    """Install a ``train_cold_start_from_csv`` that raises what the test wants."""

    def _install(exc: Exception) -> None:
        monkeypatch.setattr(
            upload_checkpoint, "train_cold_start_from_csv", _trainer_raising(exc)
        )

    return _install


async def _resolve(*, train_model: bool):
    return await resolve_upload_checkpoint(
        None, TENANT, CSV_PATH, train_model=train_model
    )


def _assert_no_shortage_prose(detail: str) -> None:
    for phrase in SHORTAGE_PHRASES:
        assert phrase not in detail.lower(), (
            f"{phrase!r} is the too-little-data answer and does not belong here: "
            f"{detail!r}"
        )


class TestWhyTheThirdPathIsUnreachable:
    """The alias, asserted as the structural fact it is.

    This is the honest form of covering the third path: it states *why* a bare
    ``ValueError`` cannot be told apart from ``InsufficientDataError``, without
    endorsing where one currently goes. It is a tripwire on the design decision
    in front of the user — if the alias ever becomes a subclass, this fails and
    the four catch sites relying on ``ValueError`` -> HTTP 400 get re-audited.
    """

    def test_insufficient_data_error_is_an_alias_not_a_subclass(self) -> None:
        """``is``, not ``issubclass`` — the distinction is the whole problem."""
        assert InsufficientDataError is ValueError

    def test_so_no_isinstance_check_can_separate_the_two(self) -> None:
        """Both directions hold, which is what "identical class" means.

        An ``except`` clause selects on class identity, so when the two names
        denote one class there is no ordering, no tuple and no ``isinstance``
        test that routes them differently.
        """
        bare = ValueError("tensor shape mismatch in layer 3")
        declared = InsufficientDataError(INSUFFICIENT_DETAIL)

        assert type(bare) is type(declared)
        assert isinstance(bare, InsufficientDataError)
        assert isinstance(declared, ValueError)

    def test_the_discard_error_by_contrast_is_a_real_subclass(self) -> None:
        """Which is precisely why path 2 *is* separable and path 3 is not."""
        assert issubclass(ExcessivePass1DiscardError, ValueError)
        assert ExcessivePass1DiscardError is not ValueError
        assert not isinstance(ValueError("x"), ExcessivePass1DiscardError)


class TestInsufficientDataOnAutoTrain:
    """Path 1 — the existing, correct behaviour. Passes today."""

    @pytest.mark.asyncio
    async def test_raises_no_model_available(self, failing_trainer) -> None:
        failing_trainer(InsufficientDataError(INSUFFICIENT_DETAIL))
        with pytest.raises(NoModelAvailableError):
            await _resolve(train_model=False)

    @pytest.mark.asyncio
    async def test_the_tenant_is_told_to_supply_more_data(
        self, failing_trainer
    ) -> None:
        failing_trainer(InsufficientDataError(INSUFFICIENT_DETAIL))
        with pytest.raises(NoModelAvailableError) as exc_info:
            await _resolve(train_model=False)

        detail = str(exc_info.value)
        assert "not enough clean data" in detail.lower()
        assert INSUFFICIENT_DETAIL in detail, "the underlying cause must survive"

    @pytest.mark.asyncio
    async def test_it_maps_to_400_at_both_call_sites(self, failing_trainer) -> None:
        failing_trainer(InsufficientDataError(INSUFFICIENT_DETAIL))
        with pytest.raises(NoModelAvailableError) as exc_info:
            await _resolve(train_model=False)

        assert isinstance(exc_info.value, ValueError)


class TestExcessiveDiscardOnForcedTrain:
    """Path 2, forced branch — already correct today: ``if train_model: raise``."""

    @pytest.mark.asyncio
    async def test_the_discard_error_propagates_unchanged(
        self, failing_trainer
    ) -> None:
        failing_trainer(ExcessivePass1DiscardError(DISCARD_DETAIL))
        with pytest.raises(ExcessivePass1DiscardError) as exc_info:
            await _resolve(train_model=True)

        assert str(exc_info.value) == DISCARD_DETAIL

    @pytest.mark.asyncio
    async def test_it_is_not_relabelled_as_a_data_shortage(
        self, failing_trainer
    ) -> None:
        failing_trainer(ExcessivePass1DiscardError(DISCARD_DETAIL))
        with pytest.raises(ExcessivePass1DiscardError) as exc_info:
            await _resolve(train_model=True)

        _assert_no_shortage_prose(str(exc_info.value))


class TestExcessiveDiscardOnAutoTrain:
    """Path 2, auto-train branch — EXPECTED-RED until backend's fix lands.

    Today this falls into ``except (InsufficientDataError, ValueError)`` and is
    wrapped in ``NoModelAvailableError``'s "there is not enough clean data in
    this upload to train one" message. That advice is actively wrong here: the
    upload may be a full year, and the tenant is being told to send more of the
    same.
    """

    @pytest.mark.asyncio
    async def test_the_discard_message_reaches_the_tenant(
        self, failing_trainer
    ) -> None:
        failing_trainer(ExcessivePass1DiscardError(DISCARD_DETAIL))
        with pytest.raises(ValueError) as exc_info:
            await _resolve(train_model=False)

        assert DISCARD_DETAIL in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_it_is_not_relabelled_as_a_data_shortage(
        self, failing_trainer
    ) -> None:
        """The relabelling itself — the defect B2 is about."""
        failing_trainer(ExcessivePass1DiscardError(DISCARD_DETAIL))
        with pytest.raises(ValueError) as exc_info:
            await _resolve(train_model=False)

        _assert_no_shortage_prose(str(exc_info.value))

    @pytest.mark.asyncio
    async def test_it_still_maps_to_400(self, failing_trainer) -> None:
        """Discrimination must not cost the 400: this is the tenant's own fault
        to fix, so it stays a client error rather than becoming a 500."""
        failing_trainer(ExcessivePass1DiscardError(DISCARD_DETAIL))
        with pytest.raises(ValueError) as exc_info:
            await _resolve(train_model=False)

        assert isinstance(exc_info.value, ValueError)


class TestUnrelatedExceptionsAreUnaffected:
    """Negative control: the existing non-``ValueError`` path still behaves.

    Without this, a fix that routed *everything* to a server error would pass
    path 3 while breaking path 1.
    """

    @pytest.mark.asyncio
    async def test_a_runtime_error_still_yields_no_model_available(
        self, failing_trainer
    ) -> None:
        failing_trainer(RuntimeError("torch exploded"))
        with pytest.raises(NoModelAvailableError) as exc_info:
            await _resolve(train_model=False)

        assert TENANT in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_forced_training_wraps_a_runtime_error_as_a_server_error(
        self, failing_trainer
    ) -> None:
        from src.api.services.upload_checkpoint import ColdStartTrainingError

        failing_trainer(RuntimeError("torch exploded"))
        with pytest.raises(ColdStartTrainingError):
            await _resolve(train_model=True)

"""The ``Pass1Warning`` trust boundary: stable codes, tenant-safe prose.

A Pass-1 warning crosses a trust boundary. ``BootstrapPass1Result.warnings``
reaches the customer through ``POST /api/v1/bootstrap/run``, is persisted in
``metrics_json``, and appears in ``web/openapi.json``. The free-text form it
replaced told the caller to "add it to ``BOOTSTRAP_MAD_EXCLUDED_CHANNELS``" —
an internal environment variable named to a customer in an API response.

So each warning is now two halves with two audiences, and this module asserts
that the split holds:

* **``code``** — drawn from the closed set ``PASS1_WARNING_CODES``, stable
  enough to branch on and to store.
* **``message``** — tenant-safe prose: no environment variable names, no
  internal constant, module, field or class names.

The remediation was not deleted, it moved to the log. That relocation is the
easy thing to undo by accident in a later edit, which is what
:class:`TestNoInternalIdentifiersLeak` exists to catch. It drives all three
codes through their real producers rather than constructing ``Pass1Warning``
objects by hand — a hand-built object would prove only that this test can
write safe prose.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.models.mad_scrub import compute_mad_scrub
from src.models.mad_scrub_report import MadScrubOptions
from src.models.pass1_discard_guard import evaluate_pass1_discard
from src.models.pass1_warning import (
    PASS1_WARNING_CODES,
    WARNING_CODE_CHANNEL_CULL_HIGH,
    WARNING_CODE_DISCARD_EXCESSIVE,
    WARNING_CODE_DISCARD_HIGH,
    Pass1Warning,
)

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
KNIFE_EDGE_FIXTURE = _FIXTURE_DIR / "pass1_light_knife_edge_high_modal.csv"

SENSOR_COLUMNS = ["temperature", "humidity", "co2", "light"]

# Internal vocabulary that must not reach a tenant. "BOOTSTRAP_" covers every
# env var in the family; the rest are internal type, field and module names
# that appeared in the prose this contract replaced, or that an author might
# reasonably reach for next.
FORBIDDEN_IN_TENANT_PROSE = (
    "BOOTSTRAP_",
    "MAD",
    "IsolationForest",
    "mad_flags",
    "mad_scrub",
    "modal_share",
    "skip_reason",
    "cull_fraction",
    "data_discard_fraction",
    "Pass1DiscardStats",
    "BootstrapPass1Result",
    "_SENSOR_COLS",
    "z-score",
    "z threshold",
)


def _channel_cull_warnings() -> tuple[Pass1Warning, ...]:
    """Real ``channel_cull_fraction_high`` warnings from the real scrub.

    Both channel guards are disabled so ``light`` is actually scrubbed on the
    knife-edge fixture and culls 49.9% of itself, well over the 10% per-channel
    limit that triggers this code.
    """
    from src.models.bootstrap import _load_and_resample

    frame = _load_and_resample(str(KNIFE_EDGE_FIXTURE))
    result = compute_mad_scrub(
        frame,
        SENSOR_COLUMNS,
        options=MadScrubOptions(excluded_columns=frozenset(), max_modal_share=1.0),
    )
    return result.warnings


def _discard_warnings(discard_fraction: float) -> tuple[Pass1Warning, ...]:
    """Real discard warnings at a chosen data-discard fraction."""
    rows = 1_000
    discarded = int(round(rows * discard_fraction))
    frame = pd.DataFrame(
        np.ones((rows, len(SENSOR_COLUMNS)), dtype=float), columns=SENSOR_COLUMNS
    )
    mask = np.zeros(rows, dtype=bool)
    mask[:discarded] = True
    return evaluate_pass1_discard(frame, pd.Series(mask), SENSOR_COLUMNS).warnings


@pytest.fixture(scope="module")
def all_real_warnings() -> dict[str, Pass1Warning]:
    """One real warning per code in the closed set, keyed by code.

    Built from the three producers, so the closed-set assertion below is a
    statement about production and not about this fixture.
    """
    collected: dict[str, Pass1Warning] = {}
    for warning in (
        _channel_cull_warnings()
        + _discard_warnings(0.20)   # >= warn, < fail
        + _discard_warnings(0.60)   # >= fail
    ):
        collected.setdefault(warning.code, warning)
    return collected


class TestClosedCodeSet:
    def test_the_set_has_exactly_the_three_documented_codes(self) -> None:
        assert PASS1_WARNING_CODES == frozenset(
            {
                WARNING_CODE_DISCARD_HIGH,
                WARNING_CODE_DISCARD_EXCESSIVE,
                WARNING_CODE_CHANNEL_CULL_HIGH,
            }
        )

    def test_all_three_codes_are_reachable_from_real_producers(
        self, all_real_warnings: dict[str, Pass1Warning]
    ) -> None:
        """A code nothing can emit is dead contract; a producer with no code is a gap."""
        assert set(all_real_warnings) == set(PASS1_WARNING_CODES)

    def test_codes_are_stable_literals(self) -> None:
        """Pinned because consumers persist these strings in ``metrics_json``.

        A rename is a data migration, not a refactor, so it should have to
        change a test that says so.
        """
        assert WARNING_CODE_DISCARD_HIGH == "discard_fraction_high"
        assert WARNING_CODE_DISCARD_EXCESSIVE == "discard_fraction_excessive"
        assert WARNING_CODE_CHANNEL_CULL_HIGH == "channel_cull_fraction_high"


class TestNoInternalIdentifiersLeak:
    """Tenant-facing prose names nothing internal. Driven by real producers."""

    def test_every_real_message_is_free_of_internal_identifiers(
        self, all_real_warnings: dict[str, Pass1Warning]
    ) -> None:
        for code, warning in all_real_warnings.items():
            for identifier in FORBIDDEN_IN_TENANT_PROSE:
                assert identifier not in warning.message, (
                    f"{identifier!r} leaked into the tenant-facing message for "
                    f"{code!r}: {warning.message!r}"
                )

    def test_every_real_message_is_non_empty_prose(
        self, all_real_warnings: dict[str, Pass1Warning]
    ) -> None:
        """A blank message would pass the leak test vacuously."""
        for code, warning in all_real_warnings.items():
            assert len(warning.message) > 40, f"{code!r} message is too terse to act on"
            assert warning.message.endswith("."), f"{code!r} message is not a sentence"

    def test_the_channel_warning_still_names_the_channel(
        self, all_real_warnings: dict[str, Pass1Warning]
    ) -> None:
        """The code repeats per channel, so the message must identify which one.

        This is the one internal-looking string that belongs in the prose: a
        sensor channel name is the tenant's own vocabulary, and without it the
        warning is unactionable.
        """
        message = all_real_warnings[WARNING_CODE_CHANNEL_CULL_HIGH].message
        assert "light" in message

    def test_the_forbidden_list_would_actually_catch_a_leak(self) -> None:
        """Negative control for the leak assertions themselves.

        Without this, a typo in ``FORBIDDEN_IN_TENANT_PROSE`` would make every
        test in this class pass against any message at all.
        """
        leaky = Pass1Warning(
            code=WARNING_CODE_DISCARD_HIGH,
            message="Raise BOOTSTRAP_MAD_Z_THRESHOLD and retry.",
        )
        assert any(
            identifier in leaky.message for identifier in FORBIDDEN_IN_TENANT_PROSE
        )


class TestSerializationShape:
    def test_as_dict_has_exactly_code_and_message(self) -> None:
        warning = Pass1Warning(code=WARNING_CODE_DISCARD_HIGH, message="Something.")
        assert warning.as_dict() == {
            "code": WARNING_CODE_DISCARD_HIGH,
            "message": "Something.",
        }

    def test_as_dict_values_are_json_scalars(
        self, all_real_warnings: dict[str, Pass1Warning]
    ) -> None:
        """``metrics_json`` is serialized with ``allow_nan=False``; strings are safe."""
        for warning in all_real_warnings.values():
            for value in warning.as_dict().values():
                assert isinstance(value, str)

    def test_warnings_are_frozen(self) -> None:
        """Immutable, so a consumer cannot edit prose that has already been logged."""
        warning = Pass1Warning(code=WARNING_CODE_DISCARD_HIGH, message="Something.")
        with pytest.raises(dataclasses.FrozenInstanceError):
            warning.message = "edited"  # type: ignore[misc]

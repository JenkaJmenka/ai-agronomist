"""``BootstrapRunResponse.pass1_warnings`` carries ``{code, message}`` objects.

The second instance of the same bug as the ``metrics_json`` break, on a
different surface and found separately. ``POST /api/v1/bootstrap/run`` builds
its response with ``pass1_warnings=list(pass1_result.warnings)``
(``src/api/routes/bootstrap.py:191``) while the schema field was declared
``list[str]`` (``src/api/schemas/bootstrap.py:38``). Pydantic does not coerce a
dataclass to ``str``, so **every run that produced any warning raised
``pydantic.ValidationError``** and returned a 500 — reproduced directly:

    ValidationError: 1 validation error for BootstrapRunResponse
    pass1_warnings.0
      Input should be a valid string [type=string_type,
      input_value=Pass1Warning(code='discard_fraction_high', ...)]

Both instances share a cause worth naming: the warning type changed from
``str`` to a dataclass in ``src/models/``, and the two *consumers* of that
type — the metrics dict and the response schema — were each written against
the old shape and neither had a test. The type change was correct; the absence
of a consumer test is what let a correct change break two surfaces silently.

So the assertions below are deliberately about the **serialized** shape rather
than about the Python objects: what a tenant receives over HTTP is the thing
that regressed, and a test on the dataclass would have stayed green through
both breaks.

**Expected-red** until backend-engineer's ``BootstrapPass1Warning`` model
lands. The fix is in ``src/api/schemas/`` and ``src/api/routes/``, neither of
which is this module's to edit.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.schemas.bootstrap import BootstrapRunResponse
from src.models.pass1_warning import (
    PASS1_WARNING_CODES,
    WARNING_CODE_CHANNEL_CULL_HIGH,
    WARNING_CODE_DISCARD_HIGH,
    Pass1Warning,
)

DISCARD_WARNING = Pass1Warning(
    code=WARNING_CODE_DISCARD_HIGH,
    message=(
        "Data cleaning removed 200 of 1000 readings (20.0%) before training, "
        "above the 15% level we flag for review."
    ),
)
CHANNEL_WARNING = Pass1Warning(
    code=WARNING_CODE_CHANNEL_CULL_HIGH,
    message=(
        "Sensor channel 'light' had 49.9% of its readings (200 of 401) removed "
        "as outliers during data cleaning."
    ),
)


def _response(*warnings: Pass1Warning) -> BootstrapRunResponse:
    """Build the response exactly as ``routes/bootstrap.py:191`` does."""
    return BootstrapRunResponse(
        rows_ingested=1_000,
        pass1_clean_rows=800,
        pass1_anomaly_rows=200,
        results=[],
        pass1_warnings=list(warnings),
    )


class TestWarningObjectsAreAccepted:
    """The route passes ``Pass1Warning`` objects; the schema must take them."""

    def test_a_single_warning_validates(self) -> None:
        """This is the assertion that was raising ``ValidationError``."""
        response = _response(DISCARD_WARNING)
        assert len(response.pass1_warnings) == 1

    def test_several_warnings_validate(self) -> None:
        """``channel_cull_fraction_high`` can repeat, so lists are the norm."""
        response = _response(CHANNEL_WARNING, CHANNEL_WARNING, DISCARD_WARNING)
        assert len(response.pass1_warnings) == 3

    def test_a_clean_run_has_an_empty_list(self) -> None:
        """Negative control, and the case that always worked.

        ``list(())`` is ``[]``, which satisfies ``list[str]`` as readily as
        anything else — which is exactly why the break stayed invisible.
        """
        assert _response().pass1_warnings == []


class TestSerializedShape:
    """What the tenant actually receives over the wire."""

    def test_each_warning_serializes_as_code_and_message(self) -> None:
        dumped = _response(DISCARD_WARNING).model_dump()

        assert dumped["pass1_warnings"] == [
            {"code": DISCARD_WARNING.code, "message": DISCARD_WARNING.message}
        ]

    def test_the_code_is_from_the_closed_set(self) -> None:
        """A consumer branching on ``code`` needs the set to be closed."""
        dumped = _response(DISCARD_WARNING, CHANNEL_WARNING).model_dump()

        for entry in dumped["pass1_warnings"]:
            assert entry["code"] in PASS1_WARNING_CODES

    def test_warnings_survive_a_json_round_trip(self) -> None:
        """``model_dump_json`` is what FastAPI serializes the body with."""
        import json

        decoded = json.loads(_response(DISCARD_WARNING).model_dump_json())

        assert decoded["pass1_warnings"][0]["code"] == WARNING_CODE_DISCARD_HIGH
        assert decoded["pass1_warnings"][0]["message"] == DISCARD_WARNING.message

    def test_a_warning_is_no_longer_a_bare_string(self) -> None:
        """Pins the shape change itself, so a revert to ``list[str]`` fails here.

        A bare string cannot be branched on without parsing prose, which is the
        problem the code/message split exists to solve.
        """
        dumped = _response(DISCARD_WARNING).model_dump()

        assert not isinstance(dumped["pass1_warnings"][0], str)
        assert set(dumped["pass1_warnings"][0]) == {"code", "message"}


class TestMalformedWarningsAreRejected:
    """The field is typed, not a free-for-all — the point of fixing it properly.

    Caveat worth stating: these two **pass today for the wrong reason.** The
    current ``list[str]`` rejects any dict, malformed or not, so they are green
    against the broken schema and will only become meaningful once the field
    accepts ``{code, message}`` and has to reject the incomplete forms
    specifically. Recorded here so nobody reads their green as evidence the
    field is already typed.
    """

    def test_a_warning_missing_its_code_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BootstrapRunResponse(
                rows_ingested=1,
                pass1_clean_rows=1,
                pass1_anomaly_rows=0,
                results=[],
                pass1_warnings=[{"message": "no code here"}],
            )

    def test_a_warning_missing_its_message_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BootstrapRunResponse(
                rows_ingested=1,
                pass1_clean_rows=1,
                pass1_anomaly_rows=0,
                results=[],
                pass1_warnings=[{"code": WARNING_CODE_DISCARD_HIGH}],
            )

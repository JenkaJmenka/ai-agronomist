"""Guarded JSON serialization for model `metrics_json`.

Two places serialize a metrics dict with `allow_nan=False`:
`src/api/services/cold_start_training.py::_build_pass1_metrics` (validation) and
`src/models/model_registry.py::register_model` (the actual write). Both used a
bare `json.dumps`, which raises `ValueError` on a non-finite value — and
`ValueError` is exactly what the cold-start path catches and maps to **HTTP 400
"not enough clean data"**. A serialization defect therefore reached the tenant
as a complaint about their upload.

`MetricsSerializationError` subclasses **`RuntimeError`, deliberately not
`ValueError`**, so it escapes `except (InsufficientDataError, ValueError)` and
surfaces as a 500. That is the honest class: a non-finite metric is a number
*we* computed, so it is our defect, not bad input. `InsufficientDataError` is
still an alias of `ValueError` and every existing 400 path is untouched.

Both sites go through :func:`dumps_metrics` so the guard cannot drift between
them — the previous arrangement had the validation and the write using
different code with the same intent, which is how the second one came to be
missing `allow_nan=False` at all.
"""

from __future__ import annotations

import json
from typing import Mapping


class MetricsSerializationError(RuntimeError):
    """A metrics dict could not be serialized to standards-compliant JSON.

    Almost always a non-finite float (NaN / Infinity) that a detector or
    aggregate produced. Subclasses ``RuntimeError`` so it is **not** caught by
    the cold-start path's ``except (InsufficientDataError, ValueError)`` and so
    reaches the client as a 500 rather than as a 400 about their data.
    """


def dumps_metrics(metrics: Mapping[str, object]) -> str:
    """Serialize *metrics* to JSON, rejecting non-finite floats.

    Returns the JSON text, so a caller that needs the string (the
    ``metrics_json`` column) and a caller that only needs validation can share
    one implementation.

    Raises:
        MetricsSerializationError: on any non-finite float, or on a value that
            is not JSON-serializable at all — e.g. a dataclass that should have
            been converted with ``as_dict()`` first.
    """
    try:
        return json.dumps(metrics, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise MetricsSerializationError(
            f"metrics_json is not serializable to valid JSON: {exc}"
        ) from exc

"""ADR-010 "last real writer owns provenance" rule for ``bulk_upsert_rows``.

See ``docs/architecture/ADR-010-synthetic-provenance-marker.md``. This module
is the sole owner of the synthetic-provenance domain knowledge used by the
upsert layer: it is the only place in ``src/db`` that imports
``SYNTHETIC_SOURCE_PREFIX`` and the only place that knows which columns carry
a detection label/score band. ``src/db/upsert.py`` stays domain-agnostic --
it asks a model whether it opts into this rule (``PROVENANCE_TRACKED``) and,
if so, takes the ``ON CONFLICT`` SET expressions from here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from sqlalchemy import case, func
from sqlalchemy.sql.elements import ColumnElement

from src.config import SYNTHETIC_SOURCE_PREFIX

if TYPE_CHECKING:
    # Type-only: the generic builder signature lives in upsert.py, the
    # "reusable mechanism" this module plugs into. Importing it here only
    # under TYPE_CHECKING avoids a runtime circular import, since upsert.py
    # imports this module to get the provenance SET expressions.
    from src.db.upsert import ColumnUpdateExprBuilder

# Columns reset to the incoming (upload) value when, and only when, the
# EXISTING row's source was synthetic and the INCOMING row's is not -- a real
# detector's label/score band (source == 'live') is left untouched, to the
# normal rescoring path.
_PROVENANCE_RESET_COLS: tuple[str, ...] = (
    "anomaly_type",
    "anomaly_score",
    "anomaly_score_raw",
    "score_scale",
    "trigger_channel",
    "trigger_detector",
    "detected_severity",
    "trigger_value",
)


def case_cols_for(
    model: type, preserve_when_null: Sequence[str], update_cols: Sequence[str]
) -> dict[str, "ColumnUpdateExprBuilder"]:
    """The ADR-010 "real data wins" rule, as ``case_update_cols`` entries.

    Approved rule (verbatim):
    - ``source``: last writer wins -- any non-NULL existing source (live or
      synthetic:*) is replaced by the incoming value (NULL for a plain CSV
      upload today). An untagged existing row is left exactly as-is.
    - ``anomaly_type`` and the synthetic scoring columns: reset to the
      incoming value ONLY when the EXISTING source was synthetic:* AND the
      INCOMING source is not. A synthetic-over-synthetic upsert (provenance
      moves, nothing real was written) must not wipe the label; a 'live'
      existing row keeps its label/scoring regardless, since those belong to
      the normal rescoring path, not to this upsert.
    - A reset-eligible column the caller explicitly named in ``update_cols``
      is excluded from this rule entirely and takes the incoming value
      unconditionally -- the caller owns that column for this write, and the
      reset must not reach back over an explicit update. An omitted column
      still goes through the reset/fallback CASE above. ``update_cols`` is
      consulted only for membership; the two branches collapse to the same
      ``excluded_col`` expression for a named column either way, so skipping
      the CASE build for it changes nothing observable except letting the
      caller's own value through unconditionally when the reset would not
      have fired on its own (e.g. live-over-synthetic).

    Called by ``bulk_upsert_rows`` only for models with
    ``PROVENANCE_TRACKED = True`` (today only ``SensorReading``) -- see that
    function's docstring for why this is not opt-in at the call site.
    """
    source_col = model.source
    is_existing_synthetic = source_col.like(f"{SYNTHETIC_SOURCE_PREFIX}%")
    is_tagged = source_col.isnot(None)

    def _source(excluded_col: ColumnElement, existing_col: ColumnElement) -> ColumnElement:
        # Any tagged (non-NULL) existing source -- live or synthetic -- is
        # replaced by the incoming value (NULL today): the uploader becomes
        # the new provenance owner. An untagged row is left exactly as-is.
        return case((is_tagged, excluded_col), else_=existing_col)

    def _make_reset_if_synthetic(col_name: str) -> "ColumnUpdateExprBuilder":
        def _builder(excluded_col: ColumnElement, existing_col: ColumnElement) -> ColumnElement:
            # excluded_col always belongs to the same per-statement "excluded"
            # pseudo-table as every other incoming column, so its sibling
            # excluded.source is reachable via .table.c -- this keeps the
            # incoming-source check working without widening the generic
            # (excluded_col, existing_col) builder signature.
            excluded_source = excluded_col.table.c.source
            # SQL's three-valued logic makes `NOT (NULL LIKE ...)` evaluate
            # to NULL, not TRUE -- a bare `~excluded_source.like(...)` would
            # make a CASE WHEN silently take the ELSE branch (no reset) for
            # a plain CSV upload, whose incoming `source` is NULL (no
            # `source` key sent). The `isnot(None)` guard forces the
            # "incoming is synthetic" test to a real boolean FALSE on NULL,
            # so NOT of it is a real TRUE and the reset still applies.
            is_incoming_synthetic = excluded_source.isnot(None) & excluded_source.like(
                f"{SYNTHETIC_SOURCE_PREFIX}%"
            )
            # Non-synthetic fallback mirrors what this column would have done
            # without the provenance rule: coalesce-preserve if the caller
            # asked for that, otherwise a no-op (leave the existing value).
            fallback = (
                func.coalesce(excluded_col, existing_col)
                if col_name in preserve_when_null
                else existing_col
            )
            reset_applies = is_existing_synthetic & ~is_incoming_synthetic
            return case((reset_applies, excluded_col), else_=fallback)

        return _builder

    case_cols: dict[str, "ColumnUpdateExprBuilder"] = {"source": _source}
    case_cols.update(
        {
            col: _make_reset_if_synthetic(col)
            for col in _PROVENANCE_RESET_COLS
            if hasattr(model, col) and col not in update_cols
        }
    )
    return case_cols

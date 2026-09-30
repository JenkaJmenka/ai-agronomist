"""Shared severity/detector ranking for cold-run upload anomaly hits.

One ranking function, used at every site that needs to pick the "most
important" hit among several candidates: trigger attribution for hits that
share a timestamp (``cold_run_upload.py``) and the display-cap sort key that
decides which rows survive a row-count limit (``anomalies_page_data.py``).
Both sites used to rank on ``|z_score|`` alone, which ranked a statistical
warning above an urgent stuck-fault freeze (stuck hits have no z_score) and
undercounted the compound ``"lstm+statistical"`` label wherever a site
matched detector by exact string equality instead of membership.
"""

from __future__ import annotations

from typing import Any, Mapping

# Detector tiers, checked by *membership* in the (possibly compound)
# detector label — never by exact string equality. "lstm+statistical" is a
# valid compound label produced when both detectors fire the same
# timestamp (see cold_run_upload.py::_merge_by_timestamp) and must rank
# with "statistical", not fall through to the unknown tier.
_STUCK_FAULT_TIER = 3
_STATISTICAL_TIER = 2
_LSTM_TIER = 1
_UNKNOWN_TIER = 0

# Backend severity vocabulary: stuck_fault emits "urgent" only; statistical
# and the compound "lstm+statistical" emit "critical"/"warning"; solo lstm
# hits never set severity (None). There is no backend "lstm" severity
# string — that is a frontend/dashboard display convention only.
_SEVERITY_RANK: dict[str | None, int] = {"urgent": 3, "critical": 2, "warning": 1}


def _field(hit: Any, name: str) -> Any:
    """Read ``name`` off ``hit``, whether it's a model instance or a dict.

    Both shapes occur at the two call sites: ``cold_run_upload.py`` ranks
    live ``UploadHighAnomalyDetail`` instances before serialization, while
    ``anomalies_page_data.py`` ranks already-serialized dict rows read back
    out of a stored ``Job.result``/``JobPortion.result`` JSON blob.
    """
    if isinstance(hit, Mapping):
        return hit.get(name)
    return getattr(hit, name, None)


def _detector_tier(detector: str | None) -> int:
    members = (detector or "").split("+")
    if "stuck_fault" in members:
        return _STUCK_FAULT_TIER
    if "statistical" in members:
        return _STATISTICAL_TIER
    if "lstm" in members:
        return _LSTM_TIER
    return _UNKNOWN_TIER


def anomaly_rank_key(hit: Any) -> tuple[int, int, float, float, str, str]:
    """Rank an anomaly hit for trigger attribution and display-cap survival.

    Order, highest first: severity > detector tier > |z| (statistical tier
    only) > score > timestamp > column. Every level after the first two is
    there because the level before it can tie, and each is chosen so the
    result stays deterministic across upload chunk sizes
    (50/288/500/5000/single) rather than depending on iteration order:

    1. Severity — urgent > critical > warning > None.
    2. Detector tier — stuck_fault > statistical (includes the compound
       "lstm+statistical") > lstm.  Matched by membership in the
       (possibly ``"+"``-joined) detector label, never exact equality.
    3. ``|z_score|`` — current-shape stuck_fault hits carry no z_score at
       all (they use ``frozen_hours_at_detection`` instead) and solo lstm
       hits have no native z either, so both are 0.0 here and this normally
       only discriminates between two statistical-tier hits. The one
       exception: a *legacy* stuck_fault row read back from a stored
       ``Job.result``/``JobPortion.result`` blob (persisted before
       ``frozen_hours_at_detection`` existed) still carries its duration in
       the numeric ``z_score`` field, so two tied legacy stuck rows compare
       by duration here — harmless for ordering (both sides are durations,
       larger still means "worse"), but this field is not exclusively a
       statistical z-score in that one case. It never reorders hits across
       tiers regardless — detector_tier already separated them by then.
    4. Canonical ``score`` — every same-tier group that isn't discriminated
       by ``|z|`` (e.g. two solo-lstm hits, which always share
       severity=None/detector="lstm"/z_score=None) would otherwise collapse
       to one identical key at this point, turning `sorted()`/`max()` into
       a no-op that returns whatever order the hits happened to arrive in.
       ``score`` is a property of the row itself, not of iteration order,
       so unlike an iteration-order tiebreak it cannot vary with where a
       chunk boundary falls. ``StatisticalAnomalyDetail`` dicts (from
       ``statistical_summary.anomalies``) have no ``score`` field at all;
       the 0.0 default for those is load-bearing, not decorative — it just
       means such rows fall through to the identity keys below rather than
       claiming a false 0.0-vs-0.0 win over each other.
    5. ``timestamp`` (as its raw string, coerced ``str(timestamp or "")`` —
       never ``None``, since comparing ``None`` against ``str`` raises
       ``TypeError`` mid-sort, and a crash here would surface as a sort
       failure deep in a display path). Larger string wins, i.e. a later
       ISO-8601 timestamp beats an earlier one under lexicographic
       comparison. One known quirk: the LSTM scoring path's placeholder for
       an out-of-range sequence index is the literal string ``"unknown"``
       (see ``upload_portions.py``'s ``lstm_high`` comprehension), which
       sorts *after* every real timestamp ('u' > any digit) — so such a row
       would win this level outright. Not special-cased: it is already
       filtered out of the trigger-attribution site (`normalize_timestamp_key`
       returns `None` for the literal "unknown" and the caller `continue`s),
       so this only matters, rarely, at the display-cap site.
    6. ``column`` (raw string, same ``str(column or "")`` coercion — solo
       lstm hits have ``column=None``). Larger string wins here too, for
       the same "pick one, document it" reason as level 5 — this is the
       true last-resort key, since a tie surviving this deep means every
       preceding field, including the row's own timestamp, was identical.

    These two are a genuine identity tiebreak (derived from the row, not
    from where it landed in a list), but the two call sites lean on
    different halves of it. At the trigger-attribution site
    (`build_trigger_data_by_timestamp`), every candidate in a group already
    shares one timestamp by construction — the group key IS derived from
    it — so level 5 never discriminates there and `column` (level 6) does
    the real work. At the display-cap site (`anomalies_page_data.py`), rows
    span the whole file, so level 5 (`timestamp`) is the one doing real
    work, with `column` as backup.

    Ties beyond all six fields (same severity, same detector tier, same |z|
    or both 0.0, same score or both missing it, same timestamp, same
    column) keep the first-encountered hit when this key is used with
    ``max()`` (which only replaces the running winner on a strict ``>``) or
    any stable sort. That remnant case is also chunk-size-invariant, for
    the same structural reason as the fields above it: portions are always
    merged in ascending ``portion_idx`` order regardless of how many rows
    each portion holds, and within a portion the detectors emit hits in the
    sensor column's fixed iteration order — neither depends on where a
    chunk boundary happened to fall.
    """
    severity = _field(hit, "severity")
    detector = _field(hit, "detector")
    z_score = _field(hit, "z_score")
    score = _field(hit, "score")
    timestamp = _field(hit, "timestamp")
    column = _field(hit, "column")
    z_rank = abs(z_score) if isinstance(z_score, (int, float)) else 0.0
    score_rank = float(score) if isinstance(score, (int, float)) else 0.0
    return (
        _SEVERITY_RANK.get(severity, 0),
        _detector_tier(detector),
        z_rank,
        score_rank,
        str(timestamp or ""),
        str(column or ""),
    )

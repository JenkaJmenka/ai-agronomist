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


def anomaly_rank_key(hit: Any) -> tuple[int, int, float, float]:
    """Rank an anomaly hit for trigger attribution and display-cap survival.

    Order, highest first:

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
    4. Canonical ``score`` — the tiebreak. Every same-tier group that isn't
       discriminated by ``|z|`` (e.g. two solo-lstm hits, which always share
       severity=None/detector="lstm"/z_score=None) would otherwise collapse
       to one identical key, turning `sorted()`/`max()` into a no-op that
       returns whatever order the hits happened to arrive in. ``score`` is
       a property of the row itself, not of iteration order, so unlike a
       first-encountered tiebreak it cannot vary with where a chunk
       boundary falls — a strictly stronger determinism guarantee.
       ``StatisticalAnomalyDetail`` dicts (from
       ``statistical_summary.anomalies``) have no ``score`` field at all;
       the 0.0 default for those is load-bearing, not decorative — it just
       means such rows fall through to the fifth tiebreak below rather than
       claiming a false 0.0-vs-0.0 win over each other.

    Ties beyond all four fields keep the first-encountered hit when this
    key is used with ``max()`` (which only replaces the running winner on a
    strict ``>``) or any stable sort — deterministic across upload chunk
    sizes for the same reason ``score`` is: portions are always merged in
    ascending ``portion_idx`` order regardless of how many rows each portion
    holds, and within a portion the detectors emit hits in the sensor
    column's fixed iteration order, neither of which depends on where a
    chunk boundary happened to fall. In practice this only matters for a
    genuine four-way tie (same severity, same detector tier, same |z| or
    both 0.0, same score or both missing it).
    """
    severity = _field(hit, "severity")
    detector = _field(hit, "detector")
    z_score = _field(hit, "z_score")
    score = _field(hit, "score")
    z_rank = abs(z_score) if isinstance(z_score, (int, float)) else 0.0
    score_rank = float(score) if isinstance(score, (int, float)) else 0.0
    return (_SEVERITY_RANK.get(severity, 0), _detector_tier(detector), z_rank, score_rank)

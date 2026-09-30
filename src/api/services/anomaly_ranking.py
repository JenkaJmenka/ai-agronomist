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


def anomaly_rank_key(hit: Any) -> tuple[int, int, float]:
    """Rank an anomaly hit for trigger attribution and display-cap survival.

    Order, highest first:

    1. Severity — urgent > critical > warning > None.
    2. Detector tier — stuck_fault > statistical (includes the compound
       "lstm+statistical") > lstm.  Matched by membership in the
       (possibly ``"+"``-joined) detector label, never exact equality.
    3. ``|z_score|`` — only statistical-tier hits carry a real z_score, so
       this only ever discriminates between two statistical-tier hits. It
       is 0.0 for stuck_fault (which uses ``frozen_hours_at_detection``,
       not z_score) and for lstm (which has no native z at all), so it
       never reorders hits across tiers.

    Ties beyond all three fields keep the first-encountered hit when this
    key is used with ``max()`` (which only replaces the running winner on a
    strict ``>``) or any stable sort. That is deterministic across upload
    chunk sizes: portions are always merged in ascending ``portion_idx``
    order regardless of how many rows each portion holds, and within a
    portion the detectors emit hits in the sensor column's fixed iteration
    order — neither depends on where a chunk boundary happened to fall.
    """
    severity = _field(hit, "severity")
    detector = _field(hit, "detector")
    z_score = _field(hit, "z_score")
    z_rank = abs(z_score) if isinstance(z_score, (int, float)) else 0.0
    return (_SEVERITY_RANK.get(severity, 0), _detector_tier(detector), z_rank)

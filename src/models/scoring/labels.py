"""ADR-014 step A8 (part 1): upload fault labels in the scoring core.

ADR-014 §1 row 21 (labelling) says that the upload path writes no
``fault_type``. The label is made at read time by
``src/api/services/fault_classification.py`` (``fc``) through
``fc._DETECTOR_TO_FAULT_TYPE``, keyed on the persisted ``trigger_detector``.
A8 moves that label into the core, so upload can write it at write time and
keep the read-time map only for legacy rows (ADR-014 A8 row).

This module is a pure, data-driven copy of that map at ``62d12cb``:

* ``LEGACY_UPLOAD_DETECTOR_FAULT_TYPES``: ``fc._DETECTOR_TO_FAULT_TYPE``.
* ``legacy_upload_fault_label``: ``fc._fault_type_from_detector``, gated on
  ``ScoringProfile.legacy_labelling``.

It is separate from ``precedence`` on purpose: ``precedence`` is row 20 (the
window rule-vote table that the stream and API paths use), while this is
row 21 and is gated on a different profile field. ``fc`` keeps its own copy
until backend-engineer switches the upload path (A8 part 2);
``tests/models/test_scoring_labels_a8.py`` proves the two equal.

No I/O, no wall clock. Like the rest of the package it must stay free of
``src.api`` / ``src.db`` / ``src.live`` imports (ADR-014 §2.1, §3 invariant 2).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Optional

from src.models.scoring.profile import LegacyLabelling, ScoringProfile

__all__ = [
    "LEGACY_UPLOAD_DETECTOR_FAULT_TYPES",
    "UNKNOWN_FAULT_TYPE",
    "legacy_upload_fault_label",
]

#: Label for a detector that flags an anomaly but gives no rule-level fault
#: evidence (``fc._UNKNOWN_KEY``). An honest "unknown" stops the read-time
#: value fallback from mislabelling an in-band reading from the LSTM
#: normalisation ranges.
UNKNOWN_FAULT_TYPE: str = "unknown"

#: ``trigger_detector`` -> ``fault_type`` (``fc._DETECTOR_TO_FAULT_TYPE``).
#: A detector that is not a key gives ``None``.
LEGACY_UPLOAD_DETECTOR_FAULT_TYPES: Mapping[str, str] = MappingProxyType({
    "spike_mad": "spike_fault",
    "out_of_bounds": "out_of_bounds",
    "stuck_fault": "stuck_fault",
    # LSTM-only, rolling z and their combination have scores but no
    # rule-level fault evidence.
    "lstm": UNKNOWN_FAULT_TYPE,
    "statistical": UNKNOWN_FAULT_TYPE,
    "lstm+statistical": UNKNOWN_FAULT_TYPE,
})


def legacy_upload_fault_label(
    detector: Optional[str], profile: ScoringProfile
) -> Optional[str]:
    """Return the upload ``fault_type`` for a ``trigger_detector`` value.

    Reproduces ``fc._fault_type_from_detector`` exactly: a known detector
    maps through ``LEGACY_UPLOAD_DETECTOR_FAULT_TYPES``; ``None`` or an
    unrecognised string returns ``None`` (the caller continues down its
    resolution chain).

    Args:
        detector: The persisted ``trigger_detector``, or ``None``.
        profile: The scoring profile. Its ``legacy_labelling`` must be
            ``LegacyLabelling.READ_TIME_TRIGGER_MAP``.

    Returns:
        The fault type, or ``None``.

    Raises:
        ValueError: If ``profile.legacy_labelling`` is another value. No
            other labelling exists in the core yet.
    """
    if profile.legacy_labelling is not LegacyLabelling.READ_TIME_TRIGGER_MAP:
        raise ValueError(
            f"profile {profile.name!r}: legacy_labelling "
            f"{profile.legacy_labelling!r} has no core labelling; only "
            f"{LegacyLabelling.READ_TIME_TRIGGER_MAP.value!r} is supported"
        )
    if detector is None:
        return None
    return LEGACY_UPLOAD_DETECTOR_FAULT_TYPES.get(detector)

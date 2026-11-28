"""ADR-014 step A8 (part 1): the core upload label equals the read-time map.

``src/models/scoring/labels.py`` copies ``_DETECTOR_TO_FAULT_TYPE`` from
``src/api/services/fault_classification.py`` (``fc``). The test imports the
backend map, so a key or value added there and not here (or here and not
there) fails the test.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from src.api.services import fault_classification as fc
from src.models.scoring import BATCH_PROFILE, PROFILES, LegacyLabelling
from src.models.scoring.labels import (
    LEGACY_UPLOAD_DETECTOR_FAULT_TYPES,
    legacy_upload_fault_label,
)

#: A detector string that is not a key on either side.
_UNRECOGNISED_DETECTOR: str = "not_a_detector"


def test_core_label_equals_backend_map_for_every_detector() -> None:
    """Every backend key, plus None and an unrecognised string, labels the same.

    The core is compared against the backend map itself and against
    ``fc._fault_type_from_detector`` (the read-time function), with the batch
    profile, which is the only one on ``READ_TIME_TRIGGER_MAP``.
    """
    assert BATCH_PROFILE.legacy_labelling is LegacyLabelling.READ_TIME_TRIGGER_MAP
    assert dict(LEGACY_UPLOAD_DETECTOR_FAULT_TYPES) == fc._DETECTOR_TO_FAULT_TYPE
    detectors: List[Optional[str]] = [*fc._DETECTOR_TO_FAULT_TYPE, None, _UNRECOGNISED_DETECTOR]
    for detector in detectors:
        expected = None if detector is None else fc._DETECTOR_TO_FAULT_TYPE.get(detector)
        got = legacy_upload_fault_label(detector, BATCH_PROFILE)
        assert got == expected, detector
        assert got == fc._fault_type_from_detector(detector), detector
    assert legacy_upload_fault_label("lstm", BATCH_PROFILE) == fc._UNKNOWN_KEY


def test_other_labelling_raises() -> None:
    """A profile with another ``legacy_labelling`` gets ``ValueError``."""
    others = [p for p in PROFILES.values()
              if p.legacy_labelling is not LegacyLabelling.READ_TIME_TRIGGER_MAP]
    assert others, "expected stream / api on another labelling"
    for profile in others:
        with pytest.raises(ValueError, match="legacy_labelling"):
            legacy_upload_fault_label("spike_mad", profile)

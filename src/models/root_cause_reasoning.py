"""Operator-facing reasoning text for matched diagnosis rules.

Split out of :mod:`src.models.root_cause_analyzer` because it has one job with
one hard rule: **never state a number the data does not support**.

A rule in :mod:`src.models.diagnosis_rules` may carry two templates — one whose
claims require a critical reading and one that makes no such claim.  The
measured value decides which is used, and it must arrive in physical units
(°C, %RH, ppm, lux).  Feeding the normalized model input here is what printed
"Humidity at 1.0%" for a row measuring 55.9 %RH.
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np

from src.config import RH_CRIT_PERCENT
from src.models.setpoint_scada_detector import humidity_reasoning_text

logger = logging.getLogger(__name__)


def format_rule_reasoning(
    match: Dict[str, Any],
    physical_sequence: Optional[np.ndarray],
    sensor_names: List[str],
) -> str:
    """Render a matched rule's reasoning for an operator.

    Args:
        match: A rule from ``DIAGNOSIS_RULES``.
        physical_sequence: The anomaly window in physical units, shape
            ``(seq_len, input_dim)``.  ``None`` when no physical window is
            available — the numeric claim is then dropped, not guessed at.
        sensor_names: Per-channel names matching the sequence columns.

    Returns:
        The rule's reasoning text.  Falls back to the rule's static
        ``description`` whenever no template's claims are supported.
    """
    measured_rh = _measured_humidity(physical_sequence, sensor_names)
    template = _select_template(match, measured_rh)
    if not template:
        return str(match.get("description", ""))
    try:
        return template.format(rh_phrase=humidity_reasoning_text(measured_rh))
    except (IndexError, KeyError, ValueError):
        logger.warning(
            "Rule %r has a description_template with unsupported "
            "placeholders; falling back to its static description",
            match.get("cause"),
        )
        return str(match.get("description", ""))


def _measured_humidity(
    physical_sequence: Optional[np.ndarray], sensor_names: List[str]
) -> Optional[float]:
    """Latest humidity in physical %RH, or ``None`` when unavailable."""
    if physical_sequence is None:
        return None
    try:
        humidity_index = sensor_names.index("humidity")
        return float(np.asarray(physical_sequence)[-1, humidity_index])
    except (IndexError, ValueError):
        return None


def _select_template(
    match: Dict[str, Any], measured_rh: Optional[float]
) -> str:
    """Pick the template whose claims the measured RH actually supports.

    The default template asserts an elevated moisture load; a saturation
    *pattern* alone does not justify that at ordinary humidity, and an unknown
    reading never does.
    """
    is_critical = measured_rh is not None and measured_rh >= RH_CRIT_PERCENT
    if is_critical:
        return str(match.get("description_template", ""))
    return str(match.get("description_template_below_critical", ""))

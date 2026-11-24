"""ADR-014 step A3: LSTM normalisation, score scale and decision per profile.

The forward pass itself is ``src.models.inference.ModelScorer``. This module
holds the three per-path policies around it, each selected by a profile
field and each a copy of what that path computes today:

* normalisation (``legacy_normalisation``, §1 row 1, until B5);
* the score scale (vectorised float32 on upload, Python float on live and
  the comprehensive pipeline);
* the decision (``lstm_decision``, §1 row 5, pending OQ2 / B9).

OQ4 is kept as it is today: live compares ``raw / ANOMALY_SCALING_FACTOR``
with ``LIVE_ANOMALY_THRESHOLD`` (``score >= threshold``), although the config
comment says live scores are raw. Do not fix it here.

Pure functions: no I/O, no clock, no module-level state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from src.models.cold_run_threshold import calibrate_threshold
from src.models.data_utils import SENSOR_RANGES, normalize_data
from src.models.inference import ModelScores
from src.models.scoring.profile import LegacyNormalisation, LstmDecisionRule, ScoringProfile


def normalise_window(
    window: np.ndarray,
    profile: ScoringProfile,
    ranges: Optional[Sequence[Tuple[float, float]]] = None,
) -> np.ndarray:
    """Turn a path's input window into the model input, as that path does today.

    Args:
        window: ``(..., input_dim)`` array. Physical units for
            ``FILE_ADAPTIVE`` and ``FIXED_SENSOR_RANGES``; already normalised
            for ``CALLER_NORMALISED`` (the API contract).
        profile: Selects the rule through ``legacy_normalisation``.
        ranges: The file's adaptive ``(min, max)`` per channel. Required for
            ``FILE_ADAPTIVE`` and ignored otherwise.

    Returns:
        A float32 array of the same shape.

    Raises:
        ValueError: ``FILE_ADAPTIVE`` without ``ranges``.
    """
    rule = profile.legacy_normalisation
    if rule is LegacyNormalisation.CALLER_NORMALISED:
        # comprehensive_pipeline.run: np.asarray(sequence, dtype=np.float32)
        return np.asarray(window, dtype=np.float32)
    if rule is LegacyNormalisation.FIXED_SENSOR_RANGES:
        # live/scoring.py: np.array(window, float32) then SENSOR_RANGES
        return normalize_data(np.asarray(window, dtype=np.float32), SENSOR_RANGES)
    if ranges is None:
        raise ValueError("file_adaptive normalisation needs the file's ranges")
    # upload_portions.score_portion: to_numpy(float32) then adaptive ranges
    return normalize_data(np.asarray(window, dtype=np.float32), list(ranges))


def scaled_scores(scores: ModelScores, profile: ScoringProfile) -> List[float]:
    """Scaled LSTM scores in the arithmetic the profile's path uses today.

    Upload (``FILE_ADAPTIVE``) clips the float32 array; live and the
    comprehensive pipeline clip one Python float per window.

    Args:
        scores: One forward pass.
        profile: The path's profile.

    Returns:
        One scaled score per window.
    """
    if profile.legacy_normalisation is LegacyNormalisation.FILE_ADAPTIVE:
        return scores.scaled_array().tolist()
    return [scores.scaled_scalar(i) for i in range(len(scores.raw))]


@dataclass(frozen=True)
class LstmThreshold:
    """The LSTM cutoff a path uses and how it was obtained.

    Attributes:
        value: The threshold on the scaled score.
        method: ``fixed``, ``adaptive``, ``fallback`` or the
            ``calibrate_threshold`` method string (``percentile``, ...).
    """

    value: float
    method: str


def lstm_threshold(
    profile: ScoringProfile,
    *,
    file_scores: Optional[Sequence[float]] = None,
    tracker_thresholds: Optional[Tuple[float, float]] = None,
) -> LstmThreshold:
    """Resolve the LSTM threshold for the profile's decision rule.

    Args:
        profile: The path's profile.
        file_scores: Every scaled score of the file
            (``FILE_PERCENTILE_ROBUST`` only; ``finalize_scores``).
        tracker_thresholds: ``(ewma.get_threshold(), pot.get_threshold())``
            (``ADAPTIVE_EWMA_POT`` only). ``None`` when the trackers could
            not be loaded, which keeps the fallback, as the pipeline does.

    Returns:
        The threshold and its method.

    Raises:
        ValueError: ``FILE_PERCENTILE_ROBUST`` without ``file_scores``.
    """
    decision = profile.lstm_decision
    if decision.rule is LstmDecisionRule.FILE_PERCENTILE_ROBUST:
        if file_scores is None:
            raise ValueError("file_percentile_robust needs the file's scores")
        value, method = calibrate_threshold(
            np.asarray(file_scores, dtype=np.float64),
            percentile=decision.percentile,
            robust_gate_z=decision.robust_gate_z,
        )
        return LstmThreshold(value=value, method=method)
    fallback = float(decision.threshold)  # type: ignore[arg-type]
    if decision.rule is LstmDecisionRule.FIXED:
        return LstmThreshold(value=fallback, method="fixed")
    if tracker_thresholds is None:
        return LstmThreshold(value=fallback, method="fallback")
    dynamic = max(tracker_thresholds[0], tracker_thresholds[1])
    if math.isfinite(dynamic):
        return LstmThreshold(value=dynamic, method="adaptive")
    return LstmThreshold(value=fallback, method="fallback")


def lstm_flag(score: float, threshold: float, profile: ScoringProfile) -> bool:
    """Whether a scaled score flags, with the comparison the path uses today.

    Live (``FIXED``) uses ``>=`` (``LiveScorer._handle_episode``); upload
    (``finalize_scores``) and the pipeline (``_run_ensemble_classify``) use
    ``>``.

    Args:
        score: Scaled LSTM score.
        threshold: From ``lstm_threshold``.
        profile: The path's profile.

    Returns:
        ``True`` when the LSTM flags the window.
    """
    if profile.lstm_decision.rule is LstmDecisionRule.FIXED:
        return score >= threshold
    return score > threshold

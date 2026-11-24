"""ADR-014 scoring core (``src/models/scoring/``, owner ml-engineer, OQ1).

Step A2 adds only the types: ``ScoringContext``, ``HistoryProvider``,
``ScoringProfile`` and the three production profiles. Nothing calls them
yet. The package must stay free of ``src.api`` / ``src.db`` / ``src.live``
imports and of wall-clock reads (ADR-014 §2.1, §3 invariant 2).
"""

from src.models.scoring.context import HistoryProvider, ScoringContext
from src.models.scoring.profile import (
    ExplainPolicy,
    LegacyEpisodeGrouping,
    LegacyLabelling,
    LegacyNormalisation,
    LegacyPrecedence,
    LegacyWallclock,
    LstmDecision,
    LstmDecisionRule,
    NoiseBaseline,
    OobPolicy,
    ScoringProfile,
    SpikeConfirm,
    SpikeConfirmMode,
    SpikePair,
)
from src.models.scoring.profiles import API_PROFILE, BATCH_PROFILE, PROFILES, STREAM_PROFILE

__all__ = [
    "API_PROFILE",
    "BATCH_PROFILE",
    "ExplainPolicy",
    "HistoryProvider",
    "LegacyEpisodeGrouping",
    "LegacyLabelling",
    "LegacyNormalisation",
    "LegacyPrecedence",
    "LegacyWallclock",
    "LstmDecision",
    "LstmDecisionRule",
    "NoiseBaseline",
    "OobPolicy",
    "PROFILES",
    "STREAM_PROFILE",
    "ScoringContext",
    "ScoringProfile",
    "SpikeConfirm",
    "SpikeConfirmMode",
    "SpikePair",
]

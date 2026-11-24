"""ADR-014 scoring core (``src/models/scoring/``, owner ml-engineer, OQ1).

Step A2 added the types: ``ScoringContext``, ``HistoryProvider``,
``ScoringProfile`` and the three production profiles. Step A3 adds the pure
detector votes (``votes``, ``window_rules``) and the LSTM policies
(``lstm``; the forward pass is ``src.models.inference.ModelScorer``). No
production path calls them yet (A4 / A6 / A7). The package must stay free of
``src.api`` / ``src.db`` / ``src.live`` imports and of wall-clock reads
(ADR-014 §2.1, §3 invariant 2).
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
from src.models.scoring.votes import VoteResult, WindowState, window_state

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
    "VoteResult",
    "WindowState",
    "window_state",
]

"""ADR-014 scoring core (``src/models/scoring/``, owner ml-engineer, OQ1).

Step A2 added the types: ``ScoringContext``, ``HistoryProvider``,
``ScoringProfile`` and the three production profiles. Step A3 added the pure
detector votes (``votes``, ``window_rules``) and the LSTM policies
(``lstm``; the forward pass is ``src.models.inference.ModelScorer``). Since
A4a (#154) ``ComprehensiveInferencePipeline._run_with_components``
(live enrichment and the inference API) gets its window votes and LSTM
cutoff from the core; its inline vote copies are gone. Step A4b adds
``precedence`` (OQ1(a)): the precedence and label table, vote ordering,
rule reasoning and band promotion, which the pipeline switches to next. Step
A7 adds ``stream_stages``: the upload stream detectors (OOB clamp-and-mark,
duration stuck, rolling z, batch spike look-ahead) as pure stages with
explicit carried state; the upload path switches to them in A7's backend half. The package must stay free of
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

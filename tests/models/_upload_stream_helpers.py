"""Shared helper: the upload look-ahead spike hits through the core stage.

Replaces the pre-A7 ``upload_portions._detect_upload_mad_spikes`` in tests that
score a whole frame (or a portion with a carried tail) for upload spikes.
"""

from __future__ import annotations

import pandas as pd

from src.config import SEQ_LEN
from src.models.scoring import BATCH_PROFILE, ScoringContext
from src.models.scoring.stream_stages import RowHit, SpikeTailState, spike_lookahead_stage

_CTX = ScoringContext(
    tenant_id="t",
    greenhouse_id="g",
    sequence_timestamp=pd.Timestamp("2026-07-01T00:00:00Z").to_pydatetime(),
    profile=BATCH_PROFILE,
)


def upload_spike_hits(
    frame: pd.DataFrame, state: SpikeTailState | None = None
) -> tuple[list[RowHit], SpikeTailState]:
    """Look-ahead spike hits of one portion, with the tail carried in and out.

    With ``state=None`` every row of ``frame`` is new (the old
    ``new_row_start=0`` call).
    """
    start = state if state is not None else SpikeTailState(tail_rows=SEQ_LEN - 1)
    return spike_lookahead_stage(frame, start, _CTX, BATCH_PROFILE)

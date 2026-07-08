"""Process-wide PyTorch CPU threading configuration.

CPU LSTM matmuls are the dominant cost of both cold-start training and portion
scoring. PyTorch sizes its intra-op thread pool from the physical core count,
but inside a container ``os.cpu_count()`` reports the host's cores, not the
cgroup CPU quota — so the pool can be mis-sized either way. ``configure_torch_threads``
makes the count explicit (from ``TORCH_NUM_THREADS``) and is safe to call once at
each process's startup (API, worker).
"""

from __future__ import annotations

import logging

from src.config import TORCH_NUM_THREADS

logger = logging.getLogger(__name__)

_configured = False


def configure_torch_threads() -> int:
    """Pin the PyTorch intra-op thread pool. Idempotent; returns the count in effect.

    ``torch.set_num_threads`` is process-global and may be called at any time, so
    calling this once per process start is sufficient. The inter-op pool is left
    at its default: it can only be set before the first parallel op and offers no
    benefit for the single sequential model used here.
    """
    global _configured
    import torch

    if _configured:
        return torch.get_num_threads()

    if TORCH_NUM_THREADS > 0:
        torch.set_num_threads(TORCH_NUM_THREADS)
    _configured = True
    logger.info(
        "PyTorch CPU threads configured: intra_op=%d interop=%d (TORCH_NUM_THREADS=%d)",
        torch.get_num_threads(),
        torch.get_num_interop_threads(),
        TORCH_NUM_THREADS,
    )
    return torch.get_num_threads()

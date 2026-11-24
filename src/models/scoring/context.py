"""ADR-014 step A2: the scoring context and the history provider protocol.

``ScoringContext`` is everything the scoring core needs to know about one
call besides the rows, the carry and the model
(docs/architecture/ADR-014-one-scoring-path.md §2.1). There is no "now":
the clock is ``sequence_timestamp``, and it is required (no default), so a
caller that has no timestamp cannot build a context.

Nothing calls these types yet (A2). The ``HistoryProvider`` protocol is the
contract the backend adapter (A5, ``api/services/scoring_history.py``) will
implement; its method set may be refined there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from src.models.scoring.profile import ScoringProfile


@runtime_checkable
class HistoryProvider(Protocol):
    """Prefetched history for one ``(tenant_id, greenhouse_id)`` scope.

    The adapter fetches it; the core only reads it and never queries a
    database (ADR-014 §2.1 ``history``). An instance is bound to one scope,
    so state keyed by ``(tenant_id, greenhouse_id)`` cannot bleed between
    greenhouses (§3 invariant 3).
    """

    def diurnal_profile(self, anchor_date: date) -> Optional[Mapping[str, Any]]:
        """Return the diurnal profile for ``anchor_date``, or ``None``."""
        ...

    def noise_clean_windows(self) -> Sequence[Any]:
        """Return the clean windows the noise baseline is calibrated on."""
        ...

    def operational_overrides(self) -> Mapping[str, Any]:
        """Return the operational config and per-scope overrides."""
        ...

    def seasonal_scaler(self) -> Optional[Any]:
        """Return the seasonal scaler, or ``None`` when there is none."""
        ...


def _require_id(name: str, value: object) -> None:
    """Raise unless ``value`` is a non-empty string."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty str, got {value!r}")


@dataclass(frozen=True, kw_only=True)
class ScoringContext:
    """One scoring call's scope, clock, profile and prefetched history.

    Attributes:
        tenant_id: Tenant the rows belong to.
        greenhouse_id: Greenhouse within the tenant.
        sequence_timestamp: Data timestamp of the scored window (its last
            row). This is the only clock the core may read. Required: there
            is no wall-clock default (§1 rows 12, 16, 26).
        profile: The path's ``ScoringProfile``.
        history: Prefetched history for this scope, or ``None`` when the
            adapter has none (for example the API with an empty carry).
        climate: Climate-computer extras for the setpoint rules, read-only.
        sensor_pairs: Dual-sensor pairs for the bias / correlation vote.
    """

    tenant_id: str
    greenhouse_id: str
    sequence_timestamp: datetime
    profile: ScoringProfile
    history: Optional[HistoryProvider] = None
    climate: Optional[Mapping[str, Any]] = None
    sensor_pairs: Tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Validate the fields and freeze the mapping-valued ones."""
        _require_id("tenant_id", self.tenant_id)
        _require_id("greenhouse_id", self.greenhouse_id)
        if not isinstance(self.sequence_timestamp, datetime):
            raise TypeError(
                "sequence_timestamp must be a datetime (the data clock); got "
                f"{type(self.sequence_timestamp).__name__}"
            )
        if not isinstance(self.profile, ScoringProfile):
            raise TypeError(f"profile must be ScoringProfile, got {type(self.profile).__name__}")
        if self.history is not None and not isinstance(self.history, HistoryProvider):
            raise TypeError("history must implement HistoryProvider")
        # Copy and wrap so a caller mutating its own dict cannot change a
        # context after construction.
        if self.climate is not None:
            object.__setattr__(self, "climate", MappingProxyType(dict(self.climate)))
        object.__setattr__(
            self,
            "sensor_pairs",
            tuple(MappingProxyType(dict(pair)) for pair in self.sensor_pairs),
        )

"""ADR-014 step A5: the database-backed ``HistoryProvider`` adapter.

The scoring core (``src/models/scoring``) only reads history through the
``HistoryProvider`` protocol and never queries a database. This module is the
backend adapter: it owns the queries ``comprehensive_pipeline`` used to run
inline (seasonal baselines, diurnal profiles) and serves them, together with
the already-merged operational config and the caller's noise clean windows,
to ``window_state(context)``.

The queries are async and the pipeline awaits them at fixed points (the OOB
gate returns before the diurnal query), so the provider is filled lazily:
until ``load_seasonal_scaler`` / ``load_diurnal_profiles`` have run, the
protocol methods return the identity scaler (``None``) and no profiles, and
the pipeline re-derives ``window_state(context)`` after each load. Query
order, cache keys and failure handling are unchanged.

``operational_overrides()`` returns the MERGED operational config
(``OperationalConfigStore.get``), as ``window_state`` requires. Whether *raw*
overrides are configured (band promotion) is a different question and stays
with the pipeline (``_band_overrides_configured``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Mapping, MutableMapping, Optional, Sequence

import numpy as np
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import DIURNAL_PROFILE_HISTORY_DAYS, DIURNAL_VOTE_MIN_DAYS
from src.db.models import SensorReading
from src.models.seasonal_scaler import (
    SeasonalScaler,
    build_scaler_from_baselines,
    get_cached_seasonal_scaler,
    put_cached_seasonal_scaler,
)

# Same logger name the pipeline logged these messages under.
logger = logging.getLogger("src.api.services.comprehensive_pipeline")

DiurnalCache = MutableMapping[tuple, Dict[str, Dict[int, tuple]]]


async def load_seasonal_scaler(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    season: str,
    op_config: Mapping[str, Any],
) -> SeasonalScaler:
    """Return a cached SeasonalScaler (factor 1.0 when baselines are sparse)."""
    cached = get_cached_seasonal_scaler(tenant_id, greenhouse_id, season)
    if cached is not None:
        return cached
    try:
        from src.api.services.seasonal_baseline_service import (
            compute_seasonal_baselines,
        )

        baselines = await compute_seasonal_baselines(
            db,
            tenant_id=tenant_id,
            greenhouse_id=greenhouse_id,
            season_map=op_config.get("season_map"),
        )
        scaler = build_scaler_from_baselines(
            baselines,
            overrides=op_config.get("seasonal_overrides"),
        )
    except Exception as exc:
        logger.warning(
            "Seasonal baseline compute failed for %s/%s: %s — using factor 1.0",
            tenant_id,
            greenhouse_id,
            exc,
        )
        scaler = SeasonalScaler()
    put_cached_seasonal_scaler(tenant_id, greenhouse_id, season, scaler)
    return scaler


async def load_diurnal_profiles(
    db: AsyncSession,
    tenant_id: str,
    greenhouse_id: str,
    anchor_ts: Optional[datetime],
    cache: DiurnalCache,
) -> Dict[str, Dict[int, tuple]]:
    """Cached diurnal profiles, auto-built from stored history on miss.

    Format: ``{"temperature": {0: (mean, std), ...}, ...}``. An empty dict is
    cached for tenants without enough history so the build query runs at most
    once per key; profiles restored from LearningState take the same cache
    slot. Failures never block the pipeline: the diurnal check is skipped.
    ``anchor_ts`` anchors the trailing window to the reading under evaluation.
    """
    from src.api.services.diurnal_profile_service import (  # noqa: PLC0415
        build_diurnal_profiles,
        persist_diurnal_profiles,
    )

    # Include the anchor date in the cache key so profiles built for
    # historical uploads (anchor = sequence timestamp) are not reused for
    # a different day's pipeline call within the same instance.
    anchor = anchor_ts if anchor_ts is not None else datetime.now(timezone.utc)
    cache_key = (tenant_id, greenhouse_id, anchor.date())
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Maturity gate: count distinct calendar days (cadence-independent)
    # in the trailing window, excluding synthetic rows.  Profiles may
    # build from DIURNAL_PROFILE_MIN_ROWS (288) but the diurnal vote
    # requires DIURNAL_VOTE_MIN_DAYS (7) to avoid false alarms from
    # under-estimated inter-day variance.
    # anchor is already set above (from anchor_ts or now()).
    since = anchor - timedelta(days=DIURNAL_PROFILE_HISTORY_DAYS)
    try:
        days_stmt = (
            select(func.count(distinct(func.date(SensorReading.timestamp))))
            .where(
                SensorReading.tenant_id == tenant_id,
                SensorReading.greenhouse_id == greenhouse_id,
                SensorReading.timestamp >= since,
                SensorReading.timestamp <= anchor,
                or_(
                    SensorReading.source.is_(None),
                    ~SensorReading.source.startswith("synthetic:"),
                ),
            )
        )
        n_days = (await db.execute(days_stmt)).scalar() or 0
        if n_days < DIURNAL_VOTE_MIN_DAYS:
            cache[cache_key] = {}
            return {}
    except Exception:
        logger.debug(
            "Diurnal maturity query failed for tenant %s (non-critical)",
            tenant_id,
            exc_info=True,
        )
        cache[cache_key] = {}
        return {}

    profiles: Dict[str, Dict[int, tuple]] = {}
    try:
        profiles = await build_diurnal_profiles(
            db, tenant_id=tenant_id, greenhouse_id=greenhouse_id, anchor=anchor
        )
    except Exception:
        logger.warning(
            "Diurnal profile build failed for tenant %s (non-critical)",
            tenant_id,
            exc_info=True,
        )
    cache[cache_key] = profiles
    if profiles:
        logger.info(
            "Auto-built diurnal profiles for tenant %s (%d sensors)",
            tenant_id,
            len(profiles),
        )
        try:
            await persist_diurnal_profiles(
                db, tenant_id=tenant_id, profiles=profiles
            )
        except Exception:
            logger.warning(
                "Diurnal profile persist failed for tenant %s (non-critical)",
                tenant_id,
                exc_info=True,
            )
    return profiles


class DbHistoryProvider:
    """``HistoryProvider`` for one ``(tenant_id, greenhouse_id)`` scope.

    Attributes are filled by the awaited ``load_*`` methods; the protocol
    methods are synchronous reads of what was loaded.
    """

    def __init__(
        self,
        tenant_id: str,
        greenhouse_id: str,
        *,
        operational_config: Mapping[str, Any],
        noise_clean_windows: Any = None,
        diurnal_cache: Optional[DiurnalCache] = None,
    ) -> None:
        """Bind the provider to a scope.

        Args:
            tenant_id: Tenant of the scope.
            greenhouse_id: Greenhouse of the scope.
            operational_config: Merged operational config for the scope.
            noise_clean_windows: Physical clean windows, or ``None``.
            diurnal_cache: The owner's diurnal cache (keys
                ``(tenant, greenhouse, date)``); the owner evicts it.
        """
        self.tenant_id = tenant_id
        self.greenhouse_id = greenhouse_id
        self._config = operational_config
        if isinstance(noise_clean_windows, np.ndarray):
            noise_clean_windows = list(noise_clean_windows)
        self._clean = noise_clean_windows
        self._cache: DiurnalCache = diurnal_cache if diurnal_cache is not None else {}
        self._scaler: Optional[SeasonalScaler] = None
        self._diurnal: Optional[Dict[str, Dict[int, tuple]]] = None
        self._diurnal_date: Optional[date] = None

    async def load_seasonal_scaler(self, db: AsyncSession, season: str) -> SeasonalScaler:
        """Fetch (or reuse the process cache of) the seasonal scaler."""
        self._scaler = await load_seasonal_scaler(
            db, self.tenant_id, self.greenhouse_id, season, self._config
        )
        return self._scaler

    async def load_diurnal_profiles(
        self, db: AsyncSession, anchor_ts: Optional[datetime]
    ) -> Dict[str, Dict[int, tuple]]:
        """Fetch (or reuse the cache of) the diurnal profiles for the anchor."""
        self._diurnal = await load_diurnal_profiles(
            db, self.tenant_id, self.greenhouse_id, anchor_ts, self._cache
        )
        self._diurnal_date = (
            anchor_ts if anchor_ts is not None else datetime.now(timezone.utc)
        ).date()
        return self._diurnal

    def diurnal_profile(self, anchor_date: date) -> Optional[Mapping[str, Any]]:
        """Loaded profiles when they were loaded for ``anchor_date``."""
        if self._diurnal is None or anchor_date != self._diurnal_date:
            return None
        return self._diurnal

    def noise_clean_windows(self) -> Sequence[Any]:
        """The caller's clean windows (empty when none)."""
        return self._clean if self._clean is not None else []

    def operational_overrides(self) -> Mapping[str, Any]:
        """The merged operational config of the scope."""
        return self._config

    def seasonal_scaler(self) -> Optional[Any]:
        """The loaded scaler, or ``None`` before it is loaded."""
        return self._scaler

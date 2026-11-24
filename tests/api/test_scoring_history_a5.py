"""ADR-014 step A5: the DB-backed HistoryProvider adapter."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from src.api.services import scoring_history
from src.api.services.scoring_history import DbHistoryProvider, load_diurnal_profiles
from src.models.scoring.context import HistoryProvider
from src.models.seasonal_scaler import SeasonalScaler

_ANCHOR = datetime(2026, 7, 2, 2, 0, tzinfo=timezone.utc)


class _FakeDb:
    """Counts ``execute`` calls; reports too few days so no profile builds."""

    def __init__(self) -> None:
        self.executes = 0

    async def execute(self, _stmt: Any) -> Any:
        self.executes += 1

        class _R:
            def scalar(self) -> int:
                return 0

        return _R()


@pytest.mark.asyncio
async def test_provider_is_keyed_by_greenhouse_and_lazy() -> None:
    """Two greenhouses share one cache dict without bleed; unloaded = identity."""
    cache: dict = {}
    a = DbHistoryProvider("t", "gh-a", operational_config={"k": 1}, diurnal_cache=cache)
    b = DbHistoryProvider("t", "gh-b", operational_config={"k": 2}, diurnal_cache=cache)
    assert isinstance(a, HistoryProvider)
    assert a.diurnal_profile(date(2026, 7, 2)) is None and a.seasonal_scaler() is None
    assert list(a.noise_clean_windows()) == []
    cache[("t", "gh-a", _ANCHOR.date())] = {"temperature": {2: (20.0, 1.0)}}
    await a.load_diurnal_profiles(_FakeDb(), _ANCHOR)  # type: ignore[arg-type]
    assert a.diurnal_profile(_ANCHOR.date()) == {"temperature": {2: (20.0, 1.0)}}
    assert a.diurnal_profile(date(2026, 7, 3)) is None
    assert b.diurnal_profile(_ANCHOR.date()) is None
    assert (a.operational_overrides()["k"], b.operational_overrides()["k"]) == (1, 2)


@pytest.mark.asyncio
async def test_diurnal_cache_hit_and_eviction() -> None:
    """A miss caches ``{}``; a hit runs no query; evicting the key re-queries."""
    cache: dict = {}
    db = _FakeDb()
    assert await load_diurnal_profiles(db, "t", "g", _ANCHOR, cache) == {}  # type: ignore[arg-type]
    assert db.executes == 1 and ("t", "g", _ANCHOR.date()) in cache
    await load_diurnal_profiles(db, "t", "g", _ANCHOR, cache)  # type: ignore[arg-type]
    assert db.executes == 1
    del cache[("t", "g", _ANCHOR.date())]
    await load_diurnal_profiles(db, "t", "g", _ANCHOR, cache)  # type: ignore[arg-type]
    assert db.executes == 2


@pytest.mark.asyncio
async def test_seasonal_scaler_query_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The baseline query runs once per (tenant, greenhouse, season)."""
    from src.api.services import seasonal_baseline_service as svc

    calls: list[tuple] = []

    async def _fake(_db: Any, **kw: Any) -> dict:
        calls.append((kw["tenant_id"], kw["greenhouse_id"]))
        return {}

    monkeypatch.setattr(svc, "compute_seasonal_baselines", _fake)
    monkeypatch.setattr(scoring_history, "get_cached_seasonal_scaler", lambda *a: _store.get(a))
    monkeypatch.setattr(scoring_history, "put_cached_seasonal_scaler", lambda *a: _store.__setitem__(a[:3], a[3]))
    _store: dict = {}
    p = DbHistoryProvider("t", "g", operational_config={})
    first = await p.load_seasonal_scaler(object(), "summer")  # type: ignore[arg-type]
    again = await p.load_seasonal_scaler(object(), "summer")  # type: ignore[arg-type]
    assert isinstance(first, SeasonalScaler) and again is first and len(calls) == 1
    assert p.seasonal_scaler() is first

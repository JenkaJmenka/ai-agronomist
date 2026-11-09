"""`ModelRegistry.register_model` must reject non-finite metric values.

Kept in its own file deliberately: Phase 3 is adding broader Pass-1 coverage
under ``tests/`` concurrently, and a separate file keeps the two from colliding
on a rebase.

Why the guard matters, given nothing on the cold-start path produces a NaN
today: ``metrics_json`` is a plain ``String`` column, so a NaN would serialize
to the non-standard literal ``NaN``, store without complaint, and only surface
on *read* — Starlette's ``JSONResponse.render`` uses ``allow_nan=False``, so
``GET /api/models/active`` and the model list would raise at response-render
time and 500 for that tenant on every call until the row was edited out of the
database. Python's own ``json.loads`` accepts ``NaN``, which is exactly why such
a row could sit undetected between the write and the first read.

The raise is now ``MetricsSerializationError``, which is a **``RuntimeError``
subclass and deliberately not a ``ValueError``**. That distinction is the whole
point of the type: the cold-start path catches
``except (InsufficientDataError, ValueError)`` and maps it to HTTP 400 "not
enough clean data", so while a bare ``json.dumps`` ``ValueError`` was raised
here, a serialization defect of *ours* reached the tenant as a complaint about
*their* upload. A non-finite metric is a number we computed, so it is our
defect and belongs in a 500.

:func:`test_the_error_is_not_a_value_error` pins the base class for that
reason. A future refactor rebasing it onto ``ValueError`` would silently
reopen the misrouting with no other test noticing.
"""

from __future__ import annotations

import math

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Base, ModelVersion
from src.db.session import AsyncSessionLocal, engine
from src.models.metrics_serialization import MetricsSerializationError
from src.models.model_registry import ModelRegistry


@pytest_asyncio.fixture(autouse=True)
async def setup_database():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def test_db() -> AsyncSession:
    async with AsyncSessionLocal() as db:
        yield db


@pytest.mark.asyncio
async def test_register_model_rejects_non_finite_metric(test_db: AsyncSession) -> None:
    """A NaN metric raises and leaves no half-written model version behind."""
    registry = ModelRegistry(test_db)

    with pytest.raises(MetricsSerializationError):
        await registry.register_model(
            "tenant_nan",
            "cold_start",
            "path/v1.pt",
            metrics={"calibration_threshold": math.nan},
        )

    # The raise happens while building the row, before `db.add`, so nothing is
    # persisted and no deployment is switched.
    rows = await test_db.execute(
        select(ModelVersion).where(ModelVersion.customer_id == "tenant_nan")
    )
    assert rows.scalars().all() == []
    assert await registry.get_active_model("tenant_nan") is None


@pytest.mark.asyncio
async def test_the_error_is_not_a_value_error(test_db: AsyncSession) -> None:
    """The base class is the contract, not an implementation detail.

    ``MetricsSerializationError`` must stay outside ``ValueError`` so it
    escapes the cold-start path's ``except (InsufficientDataError, ValueError)``
    and surfaces as a 500. Asserted on a real raise rather than on the class
    alone, so it covers what ``register_model`` actually propagates.
    """
    registry = ModelRegistry(test_db)

    with pytest.raises(MetricsSerializationError) as exc_info:
        await registry.register_model(
            "tenant_nan",
            "cold_start",
            "path/v1.pt",
            metrics={"calibration_threshold": math.nan},
        )

    assert not isinstance(exc_info.value, ValueError)
    assert isinstance(exc_info.value, RuntimeError)

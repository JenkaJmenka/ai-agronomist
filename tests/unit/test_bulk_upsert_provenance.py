"""ADR-010 provenance rules for ``bulk_upsert_rows`` — see ``src/db/provenance.py``.

Split out of ``test_bulk_upsert.py`` for the 300-line guardrail; the engine
fixture and conflict-key timestamp are shared via
``tests/unit/_bulk_upsert_fixtures.py``.
"""

from typing import Sequence

import pytest
from sqlalchemy import select

from src.config import SYNTHETIC_SOURCE_PREFIX
from src.db.models import RawReading, SensorReading
from src.db.upsert import bulk_upsert_rows
from tests.unit._bulk_upsert_fixtures import BASE_TS, session  # noqa: F401


class TestProvenanceResetCondition:
    """The ADR-010 reset is conditional on BOTH sides of the write.

    The approved rule resets ``anomaly_type`` and the synthetic scoring columns
    only when the EXISTING row is synthetic AND the INCOMING row is not. Both
    tests here drive ``bulk_upsert_rows`` with the arguments the upload path
    passes (``src/api/routes/upload.py`` — the four sensor floats in
    ``update_cols``, ``anomaly_score`` preserved when NULL), differing only in
    whether the incoming row carries a synthetic ``source``.
    """

    SYNTHETIC_A = f"{SYNTHETIC_SOURCE_PREFIX}a"
    SYNTHETIC_B = f"{SYNTHETIC_SOURCE_PREFIX}b"

    EXISTING_LABEL = "co2_surge"
    EXISTING_SCORE_SCALE = "synthetic"
    EXISTING_SCORE = 0.9
    EXISTING_TRIGGER_CHANNEL = "temperature"

    INCOMING_LABEL = "humidity_spike"

    # What src/api/routes/upload.py names in update_cols.
    SENSOR_FLOAT_COLS: tuple[str, ...] = ("temperature", "humidity", "co2", "light")

    @staticmethod
    def _seed_synthetic_reading(source: str) -> SensorReading:
        """A row in the state a synthetic writer leaves behind."""
        return SensorReading(
            tenant_id="tenant_upsert",
            greenhouse_id="default",
            timestamp=BASE_TS,
            temperature=21.0,
            humidity=70.0,
            co2=1480.0,
            light=120.0,
            source=source,
            anomaly_type=TestProvenanceResetCondition.EXISTING_LABEL,
            score_scale=TestProvenanceResetCondition.EXISTING_SCORE_SCALE,
            anomaly_score=TestProvenanceResetCondition.EXISTING_SCORE,
            trigger_channel=TestProvenanceResetCondition.EXISTING_TRIGGER_CHANNEL,
        )

    @staticmethod
    async def _upsert(
        session, incoming: dict, update_cols: Sequence[str] = SENSOR_FLOAT_COLS
    ) -> None:
        await bulk_upsert_rows(
            session,
            SensorReading,
            [incoming],
            update_cols=update_cols,
            preserve_when_null=["anomaly_score"],
        )
        await session.commit()
        session.expire_all()

    @staticmethod
    async def _stored_row(session) -> SensorReading:
        rows = (
            await session.execute(
                select(SensorReading).where(SensorReading.timestamp == BASE_TS)
            )
        ).scalars().all()
        assert len(rows) == 1, f"expected the upsert to update in place, found {len(rows)} rows"
        return rows[0]

    async def test_should_keep_the_label_and_scoring_when_a_synthetic_row_overwrites_a_synthetic_row(
        self, session
    ):
        """Synthetic over synthetic: provenance moves, the label/scoring stay.

        The reset exists so a real measured value never carries a fabricated
        label. Nothing real was written here — only the identity of the
        synthetic writer changed — so there is nothing to reset. Keying the
        reset on the existing row's source alone wipes a label the incoming
        row never replaced.
        """
        session.add(self._seed_synthetic_reading(self.SYNTHETIC_A))
        await session.commit()

        await self._upsert(
            session,
            {
                "tenant_id": "tenant_upsert",
                "greenhouse_id": "default",
                "timestamp": BASE_TS,
                "temperature": 22.5,
                "humidity": 65.0,
                "co2": 1510.0,
                "light": 140.0,
                "source": self.SYNTHETIC_B,
            },
        )

        row = await self._stored_row(session)
        assert row.source == self.SYNTHETIC_B, "last writer owns provenance"
        assert row.anomaly_type == self.EXISTING_LABEL, (
            "the reset applies only when the incoming row is NOT synthetic"
        )
        assert row.score_scale == self.EXISTING_SCORE_SCALE
        assert row.anomaly_score == pytest.approx(self.EXISTING_SCORE)
        assert row.trigger_channel == self.EXISTING_TRIGGER_CHANNEL

    async def test_should_reset_the_label_and_scoring_when_a_plain_upload_overwrites_a_synthetic_row(
        self, session
    ):
        """The other half of the condition: real data over synthetic resets.

        Pinned alongside its sibling so a fix cannot satisfy one and break the
        other. A CSV upload carries no ``source`` key at all.
        """
        session.add(self._seed_synthetic_reading(self.SYNTHETIC_A))
        await session.commit()

        await self._upsert(
            session,
            {
                "tenant_id": "tenant_upsert",
                "greenhouse_id": "default",
                "timestamp": BASE_TS,
                "temperature": 24.25,
                "humidity": 58.5,
                "co2": 455.0,
                "light": 615.0,
            },
        )

        row = await self._stored_row(session)
        assert row.source is None, "an overwritten synthetic row is real data"
        assert row.anomaly_type is None
        assert row.score_scale is None
        assert row.anomaly_score is None
        assert row.trigger_channel is None

    async def test_should_take_the_incoming_label_when_the_caller_names_it_in_update_cols(
        self, session
    ):
        """A provenance column the caller asked to update takes the new value.

        The reset exists to stop a *stale* fabricated label outliving the write
        that replaced its row. It must not reach back over a caller who named
        the column in ``update_cols`` and sent a value for it — that would make
        the rule silently swallow a synthetic writer's own label, which is the
        contract any future synthetic writer going through this helper depends
        on.

        Both halves of the CASE are asserted here together so they cannot drift
        apart: ``anomaly_type`` is passed and must move, ``trigger_channel`` is
        omitted and must stay.
        """
        session.add(self._seed_synthetic_reading(self.SYNTHETIC_A))
        await session.commit()

        await self._upsert(
            session,
            {
                "tenant_id": "tenant_upsert",
                "greenhouse_id": "default",
                "timestamp": BASE_TS,
                "temperature": 22.5,
                "humidity": 65.0,
                "co2": 1510.0,
                "light": 140.0,
                "source": self.SYNTHETIC_B,
                "anomaly_type": self.INCOMING_LABEL,
            },
            update_cols=[*self.SENSOR_FLOAT_COLS, "anomaly_type"],
        )

        row = await self._stored_row(session)
        assert row.anomaly_type == self.INCOMING_LABEL, (
            "a column named in update_cols must take the incoming value; "
            "the provenance reset must not clobber an explicit update"
        )
        assert row.trigger_channel == self.EXISTING_TRIGGER_CHANNEL, (
            "an omitted provenance column still keeps its existing value"
        )


class TestProvenanceTrackedOptIn:
    """``PROVENANCE_TRACKED`` is a correctness dependency, not a free choice.

    ``src/db/models.py`` declares the flag on ``SensorReading`` and on nothing
    else, so ``bulk_upsert_rows`` short-circuits ``auto_case_cols`` to ``{}``
    for ``RawReading`` (``src/db/upsert.py``) and the CSV upload's
    ``source='csv'`` marker is written by the plain ``update_cols`` path.

    Flipping the flag on for ``RawReading`` "for symmetry" would silently stop
    that marker being written on re-upload.  ``provenance.case_cols_for`` adds
    ``"source"`` to ``case_cols`` **unconditionally**, and a column in
    ``merged_case_cols`` reaches the SET clause even when the caller never
    named it; ``_source``'s CASE is ``WHEN existing.source IS NOT NULL THEN
    excluded.source ELSE existing.source``, so an existing row whose source is
    still NULL -- every row uploaded before the collision fix -- keeps NULL
    forever.  That is exactly the self-healing re-upload path
    ``docs/handoff/source_column_collision_backfill.md`` leans on.

    Recorded here because a handoff doc is the only other place it is written
    down, and docs do not fail a test run.
    """

    def test_should_not_opt_raw_reading_into_the_provenance_case_rule(self) -> None:
        """``RawReading`` must stay opted OUT; ``SensorReading`` stays opted in."""
        assert not getattr(RawReading, "PROVENANCE_TRACKED", False), (
            "opting RawReading in would route source through provenance._source, "
            "whose ELSE branch preserves NULL and would stop the 'csv' marker "
            "being written on re-upload of a pre-fix row"
        )
        assert getattr(SensorReading, "PROVENANCE_TRACKED", False) is True, (
            "SensorReading is the one model ADR-010's reset rule applies to"
        )

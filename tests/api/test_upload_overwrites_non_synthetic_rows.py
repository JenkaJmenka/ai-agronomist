"""Acceptance tests for the converse of "real data wins": no collateral damage.

Split out of ``test_upload_overwrites_synthetic_rows.py`` (300-line guardrail)
— see that module's docstring for the N1 defect and the approved rule.  This
file covers the rows the rule must *not* over-reach into: the other non-NULL
``source`` (``'live'``), and a plain ``source IS NULL`` CSV row.  A fix that
clears provenance indiscriminately, or that stamps a marker of its own, is red
here while the synthetic half stays green.

Row builders, the upload upsert and tenant constants come from
``tests/api/_marker_fixtures.py`` — a helpers module both files import
explicitly, so neither reaches into the other's internals.  ``db_session``
comes from ``tests/api/conftest.py``, where pytest discovers it without an
import.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api._marker_fixtures import (
    LIVE_SOURCE,
    REAL_DETECTOR_LABEL,
    REAL_VALUES,
    UPLOAD_T,
    measured_values_of,
    only_upload_row,
    upload_collision_row,
    upload_upsert,
)


# ---------------------------------------------------------------------------
# The other non-NULL source: 'live' is overwritten too, but keeps its scoring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_should_clear_the_live_marker_but_keep_live_scoring_on_an_upload(
    db_session: AsyncSession,
) -> None:
    """The decided rule: **last real writer owns provenance.**

    An upload upsert that lands on *any* row whose ``source`` is not NULL takes
    that row's provenance — it becomes the upload's own value, NULL today.  A
    surviving ``source='live'`` would mean "Remove Go Live data" later deletes
    CSV-uploaded real values, the same data-loss class as the synthetic case
    (N1), so it is closed by the same rule.

    Only ``synthetic:%`` rows get ``anomaly_type`` and the synthetic scoring
    columns reset.  A ``live`` row keeps both: its label is a real detector's
    verdict and its score band belongs to the normal rescoring path, not to the
    upsert.

    FAILS pre-fix — this test previously passed, because today's upsert touches
    neither ``source`` nor ``anomaly_type``.  It is part of the by-design
    failing set until backend lands the rule.
    """
    db_session.add(
        upload_collision_row(
            source=LIVE_SOURCE, anomaly_type=REAL_DETECTOR_LABEL, score_scale="live"
        )
    )
    await db_session.commit()

    await upload_upsert(db_session, UPLOAD_T)
    db_session.expire_all()

    row = await only_upload_row(db_session)
    assert measured_values_of(row) == REAL_VALUES
    assert row.source is None, (
        "the upload is the last real writer; a surviving 'live' marker would let "
        "\"Remove Go Live data\" delete these CSV-uploaded real values"
    )
    assert row.anomaly_type == REAL_DETECTOR_LABEL, (
        "a real detector's label is only reset for synthetic:% rows"
    )
    assert row.score_scale == "live", (
        "live scoring columns are left to the normal rescoring path"
    )


@pytest.mark.asyncio
async def test_should_leave_a_source_null_row_untagged_when_an_upload_overwrites_it(
    db_session: AsyncSession,
) -> None:
    """A plain CSV row (``source IS NULL``) is updated in place, nothing added.

    PASSES pre-fix.  Guards against a fix that writes a marker of its own, or
    that stamps ``anomaly_type`` on re-upload.
    """
    db_session.add(upload_collision_row(source=None, anomaly_type=None, score_scale=None))
    await db_session.commit()

    await upload_upsert(db_session, UPLOAD_T)
    db_session.expire_all()

    row = await only_upload_row(db_session)
    assert measured_values_of(row) == REAL_VALUES
    assert row.source is None
    assert row.anomaly_type is None

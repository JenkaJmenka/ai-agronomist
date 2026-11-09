"""``metrics_json`` shaping for Pass-1 observability (BACKLOG-14).

Nothing in ``tests/`` referenced ``_build_pass1_metrics``, ``_mad_channel_entry``
or ``_finite_or_none`` before this module. That absence is the sharpest argument
for this whole round: ``_build_pass1_metrics`` is **currently broken on this
branch** — it passes raw ``Pass1Warning`` dataclasses into ``json.dumps``, which
raises ``TypeError`` — and the ``src/api`` suite is green anyway, because no test
calls the function. A total failure on the cold-start path was invisible.

Two things are worth separating carefully here:

* ``_finite_or_none`` exists because ``metrics_json`` is written to a JSON
  column and production is Postgres, which **rejects** the bare ``NaN`` token
  that SQLite accepts (``project_sqlite_postgres_nan_gap``). It is not a
  defensive nicety: ``light`` is always on the exclusion list, so it is always
  skipped, so its ``median``/``mad``/``modal_share`` are **always NaN**. This
  helper runs on the real path of every single cold-start run.
* ``json.dumps(..., allow_nan=False)`` inside the builder is the guard that
  makes a future non-finite value fail loudly where it is introduced, rather
  than being written as invalid JSON and failing on a production INSERT. The
  round-trip tests below assert the guard is real, not decorative.

**Expected-red.** ``test_warnings_serialize_as_code_message_dicts`` and its two
companions in :class:`TestWarningSerialization` are written against the intended
post-fix behaviour (``[w.as_dict() for w in ...]``) and fail until
backend-engineer's branch lands. That is deliberate and was agreed: the fix is
in ``src/api/**``, which is not this module's to edit.
"""

from __future__ import annotations

import json
import math

import pytest

from src.api.services.cold_start_training import (
    _build_pass1_metrics,
    _discard_stats_entry,
    _finite_or_none,
    _mad_channel_entry,
)
from src.models.bootstrap import BootstrapPass1Result
from src.models.metrics_serialization import MetricsSerializationError
from src.models.mad_scrub_report import (
    SKIP_REASON_EXCLUDED,
    SKIP_REASON_NO_SPREAD,
    MadChannelReport,
)
from src.models.pass1_discard_guard import (
    DISCARD_STATUS_OK,
    DISCARD_STATUS_WARN,
    Pass1DiscardStats,
)
from src.models.pass1_warning import WARNING_CODE_DISCARD_HIGH, Pass1Warning

import pandas as pd

CLEAN_ROWS = 100_075
ANOMALY_ROWS = 4_846
CALIBRATION_THRESHOLD = 0.0123


def _scrubbed_report(column: str, *, cull_rows: int = 0) -> MadChannelReport:
    """A channel that was really evaluated — every statistic finite."""
    finite_rows = 104_921
    return MadChannelReport(
        column=column,
        is_skipped=False,
        skip_reason=None,
        median=80.29,
        mad=5.71,
        modal_share=0.07999,
        finite_rows=finite_rows,
        cull_rows=cull_rows,
        cull_fraction=cull_rows / finite_rows,
    )


def _skipped_report(column: str, reason: str) -> MadChannelReport:
    """A skipped channel — median/mad/modal_share are NaN, as production has them."""
    return MadChannelReport(
        column=column,
        is_skipped=True,
        skip_reason=reason,
        median=float("nan"),
        mad=float("nan"),
        modal_share=float("nan"),
        finite_rows=104_921,
        cull_rows=0,
        cull_fraction=0.0,
    )


def _pass1_result(
    *,
    channels: tuple[MadChannelReport, ...],
    warnings: tuple[Pass1Warning, ...] = (),
    status: str = DISCARD_STATUS_OK,
) -> BootstrapPass1Result:
    empty = pd.DataFrame()
    flags = pd.Series([], dtype=bool)
    return BootstrapPass1Result(
        clean_df=empty,
        anomaly_df=empty,
        physics_flags=flags,
        mad_flags=flags,
        if_flags=flags,
        gas_mask_cols=frozenset(),
        mad_channels=channels,
        if_flag_fraction=0.0400015249,
        discard_stats=Pass1DiscardStats(
            frame_rows=104_921,
            frame_discarded=4_846,
            frame_discard_fraction=0.0461871313,
            data_rows=104_921,
            data_discarded=4_846,
            data_discard_fraction=0.0461871313,
            status=status,
            warnings=warnings,
        ),
        warnings=warnings,
    )


def _build(**kwargs) -> dict[str, object]:
    return _build_pass1_metrics(
        _pass1_result(**kwargs),
        clean_rows=CLEAN_ROWS,
        anomaly_rows=ANOMALY_ROWS,
        calibration_threshold=CALIBRATION_THRESHOLD,
    )


# The production channel set: `light` is always excluded and therefore always
# NaN, and `co2` is constant whenever the CSV carried no co2 column.
PRODUCTION_CHANNELS = (
    _scrubbed_report("temperature"),
    _scrubbed_report("humidity", cull_rows=1_426),
    _skipped_report("co2", SKIP_REASON_NO_SPREAD),
    _skipped_report("light", SKIP_REASON_EXCLUDED),
)


class TestFiniteOrNone:
    """Every non-finite float becomes ``None`` before it reaches a JSON column."""

    def test_nan_becomes_none(self) -> None:
        assert _finite_or_none(float("nan")) is None

    def test_positive_infinity_becomes_none(self) -> None:
        assert _finite_or_none(float("inf")) is None

    def test_negative_infinity_becomes_none(self) -> None:
        assert _finite_or_none(float("-inf")) is None

    @pytest.mark.parametrize("value", [0.0, -0.0, 1.35, -12.5, 1e-9, 1e300])
    def test_finite_values_pass_through_unchanged(self, value: float) -> None:
        """Including 0.0, which a truthiness-based guard would wrongly drop."""
        assert _finite_or_none(value) == value

    def test_zero_is_not_confused_with_absent(self) -> None:
        """``mad == 0.0`` is a real, meaningful measurement on a constant channel.

        A guard written as ``value or None`` would turn it into ``null`` and
        erase the zero-variance signal the precedence swap exists to report.
        """
        assert _finite_or_none(0.0) is not None


class TestMadChannelEntry:
    def test_a_skipped_channel_has_null_statistics(self) -> None:
        entry = _mad_channel_entry(_skipped_report("light", SKIP_REASON_EXCLUDED))

        assert entry["median"] is None
        assert entry["mad"] is None
        assert entry["modal_share"] is None

    def test_a_skipped_channel_keeps_its_reason_and_counts(self) -> None:
        """The reason is the point of the entry; nulling it would lose the record."""
        entry = _mad_channel_entry(_skipped_report("light", SKIP_REASON_EXCLUDED))

        assert entry["is_skipped"] is True
        assert entry["skip_reason"] == SKIP_REASON_EXCLUDED
        assert entry["cull_rows"] == 0
        assert entry["finite_rows"] == 104_921

    def test_a_scrubbed_channel_keeps_its_statistics(self) -> None:
        """Negative control: the helper nulls non-finite values, not all of them."""
        entry = _mad_channel_entry(_scrubbed_report("humidity", cull_rows=1_426))

        assert entry["median"] == 80.29
        assert entry["mad"] == 5.71
        assert entry["modal_share"] == 0.07999
        assert entry["skip_reason"] is None
        assert entry["cull_rows"] == 1_426


class TestDiscardStatsEntry:
    def test_both_denominators_are_published(self) -> None:
        """A consumer must never have to guess which denominator it is reading."""
        entry = _discard_stats_entry(_pass1_result(channels=()).discard_stats)

        assert entry["frame_rows"] == 104_921
        assert entry["data_rows"] == 104_921
        assert entry["frame_discarded"] == 4_846
        assert entry["data_discarded"] == 4_846
        assert entry["status"] == DISCARD_STATUS_OK


class TestBuildPass1Metrics:
    """The whole dict, round-tripped the way the database will see it."""

    def test_round_trips_through_json_with_nan_rejected(self) -> None:
        """The production channel set includes two always-NaN channels."""
        metrics = _build(channels=PRODUCTION_CHANNELS)
        encoded = json.dumps(metrics, allow_nan=False)

        assert json.loads(encoded) == metrics

    def test_the_encoded_payload_contains_no_nan_token(self) -> None:
        """``NaN`` is invalid JSON; Postgres rejects it where SQLite accepts it."""
        encoded = json.dumps(_build(channels=PRODUCTION_CHANNELS))

        assert "NaN" not in encoded
        assert "Infinity" not in encoded

    def test_skipped_channels_appear_with_null_statistics(self) -> None:
        decoded = json.loads(json.dumps(_build(channels=PRODUCTION_CHANNELS)))
        light = decoded["pass1_mad_channels"]["light"]

        assert light["median"] is None
        assert light["skip_reason"] == SKIP_REASON_EXCLUDED

    def test_channels_are_keyed_by_name_not_position(self) -> None:
        """Phase 2's contract: ``mad_channels`` order follows the request."""
        decoded = _build(channels=PRODUCTION_CHANNELS)

        assert set(decoded["pass1_mad_channels"]) == {
            "temperature",
            "humidity",
            "co2",
            "light",
        }

    def test_row_counts_and_threshold_are_carried_through(self) -> None:
        metrics = _build(channels=PRODUCTION_CHANNELS)

        assert metrics["pass1_clean_rows"] == CLEAN_ROWS
        assert metrics["pass1_anomaly_rows"] == ANOMALY_ROWS
        assert metrics["calibration_threshold"] == CALIBRATION_THRESHOLD
        assert metrics["source"] == "cold_start_upload"

    @staticmethod
    def _report_with_infinite_cull_fraction() -> MadChannelReport:
        """A report carrying a non-finite value in an *uncovered* field.

        ``_finite_or_none`` already coerces every statistic it covers, so the
        non-finite value is driven in through ``cull_fraction``, which it does
        **not** cover. That is what proves the serialization guard would catch
        a future gap here rather than at a production INSERT.
        """
        return MadChannelReport(
            column="temperature",
            is_skipped=False,
            skip_reason=None,
            median=20.0,
            mad=1.0,
            modal_share=0.1,
            finite_rows=100,
            cull_rows=0,
            cull_fraction=float("inf"),
        )

    def test_an_infinite_statistic_is_rejected_at_build_time(self) -> None:
        """The serialization guard must be load-bearing, not decorative."""
        with pytest.raises(MetricsSerializationError):
            _build(channels=(self._report_with_infinite_cull_fraction(),))

    def test_the_build_time_rejection_is_not_a_value_error(self) -> None:
        """The base class is the contract at *this* site above all others.

        ``_build_pass1_metrics`` sits on the auto-train path, where
        ``upload_checkpoint.py`` catches ``except (InsufficientDataError,
        ValueError)``. If this exception were ever a ``ValueError`` again, a
        serialization defect of ours would be relabelled to the tenant as "not
        enough clean data" about their upload — the exact misrouting this round
        removed. ``RuntimeError`` is what keeps it a 500.
        """
        with pytest.raises(MetricsSerializationError) as exc_info:
            _build(channels=(self._report_with_infinite_cull_fraction(),))

        assert not isinstance(exc_info.value, ValueError)
        assert isinstance(exc_info.value, RuntimeError)

    def test_an_empty_channel_set_still_builds(self) -> None:
        """An empty frame yields no channel reports; the builder must survive it."""
        metrics = _build(channels=())

        assert metrics["pass1_mad_channels"] == {}
        assert json.loads(json.dumps(metrics, allow_nan=False)) == metrics


class TestWarningSerialization:
    """EXPECTED-RED until backend-engineer's ``as_dict()`` fix lands.

    ``cold_start_training.py`` currently does ``list(pass1_result.warnings)``,
    which puts ``Pass1Warning`` dataclasses into the dict, and the
    ``json.dumps`` on the next line raises
    ``TypeError: Object of type Pass1Warning is not JSON serializable``.

    Note the failure is **conditional on there being a warning at all**:
    ``list(())`` is ``[]``, so every test above passes today. That is precisely
    why the break went unnoticed — the common case is warning-free, and the
    only runs that break are the ones already in trouble.
    """

    WARNING = Pass1Warning(
        code=WARNING_CODE_DISCARD_HIGH,
        message="Data cleaning removed 200 of 1000 readings (20.0%) before training.",
    )

    def test_warnings_serialize_as_code_message_dicts(self) -> None:
        metrics = _build(
            channels=PRODUCTION_CHANNELS,
            warnings=(self.WARNING,),
            status=DISCARD_STATUS_WARN,
        )

        assert metrics["pass1_warnings"] == [
            {"code": WARNING_CODE_DISCARD_HIGH, "message": self.WARNING.message}
        ]

    def test_a_result_carrying_warnings_round_trips_through_json(self) -> None:
        metrics = _build(
            channels=PRODUCTION_CHANNELS,
            warnings=(self.WARNING,),
            status=DISCARD_STATUS_WARN,
        )
        encoded = json.dumps(metrics, allow_nan=False)

        assert json.loads(encoded)["pass1_warnings"][0]["code"] == (
            WARNING_CODE_DISCARD_HIGH
        )

    def test_a_warn_status_is_persisted_rather_than_treated_as_failure(self) -> None:
        """Phase 2's contract: ``warn`` returns normally and must be stored."""
        metrics = _build(
            channels=PRODUCTION_CHANNELS,
            warnings=(self.WARNING,),
            status=DISCARD_STATUS_WARN,
        )

        assert metrics["pass1_discard"]["status"] == DISCARD_STATUS_WARN
        assert len(metrics["pass1_warnings"]) == 1


class TestNoWarningsIsTheCommonCase:
    """Pins why the break above hid: the warning-free path is fine today."""

    def test_an_empty_warning_tuple_serializes_as_an_empty_list(self) -> None:
        metrics = _build(channels=PRODUCTION_CHANNELS, warnings=())

        assert metrics["pass1_warnings"] == []
        assert math.isfinite(metrics["pass1_if_flag_fraction"])

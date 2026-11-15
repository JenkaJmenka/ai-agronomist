"""Leave-one-day-out hour-of-day temperature residual. Unwired.

``DiurnalProfileDetector`` scores one reading against a caller-supplied
hourly ``(mean, std)``. It does not build that profile, does not keep the
scored day out of the center, and does not require the excess to persist.
A residual of that kind flags clean July weather. This helper is the
check for a multi-day level bias a rolling z-score never sees: each sample
is compared to the median of the other days at the same clock hour, and a
sample flags only after a run of consecutive hourly exceedances.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

import numpy as np

from src.config import (
    HOUR_RESIDUAL_EXCESS_C,
    HOUR_RESIDUAL_MIN_OTHER_DAYS,
    HOUR_RESIDUAL_PERSISTENCE_HOURS,
)

_HourKey = tuple[date, int]
_Sample = tuple[int, date, int, float]


class HourOfDayResidualDetector:
    """Flag a temperature series after persistent same-hour excesses.

    The clock hour is the timestamp's own wall clock. Naive and aware
    timestamps are not converted. The scored calendar day is excluded
    from the center for that hour.
    """

    def __init__(
        self,
        excess_c: float = HOUR_RESIDUAL_EXCESS_C,
        persistence_hours: int = HOUR_RESIDUAL_PERSISTENCE_HOURS,
        min_other_days: int = HOUR_RESIDUAL_MIN_OTHER_DAYS,
    ) -> None:
        if excess_c <= 0:
            raise ValueError(f"excess_c must be > 0, got {excess_c}")
        if persistence_hours < 1:
            raise ValueError(
                f"persistence_hours must be >= 1, got {persistence_hours}"
            )
        if min_other_days < 1:
            raise ValueError(
                f"min_other_days must be >= 1, got {min_other_days}"
            )
        self._excess_c = float(excess_c)
        self._persistence_hours = int(persistence_hours)
        self._min_other_days = int(min_other_days)

    def detect(
        self,
        timestamps: Sequence[datetime],
        temperatures: Sequence[float],
    ) -> list[bool]:
        """Flag samples that exceed the other-days hour center persistently.

        An hour exceeds when the median of its finite samples is more than
        ``excess_c`` away from the median of that clock hour on other days.
        Samples in that hour flag only once the exceedance has run for
        ``persistence_hours`` consecutive clock hours, and only when the
        sample itself also exceeds the same center. A missing hour breaks
        the run. Non-finite samples are never flagged and never enter a
        median.

        Args:
            timestamps: One timestamp per temperature sample.
            temperatures: Physical temperature, one value per timestamp.

        Returns:
            One boolean per input sample, in input order.
        """
        times, values = _aligned_inputs(timestamps, temperatures)
        samples = _finite_samples(times, values)
        medians = _hour_medians(samples)
        centers = _leave_one_day_centers(medians, self._min_other_days)
        exceeding = _exceeding_hours(medians, centers, self._excess_c)
        flagged_hours = _persistent_hours(exceeding, self._persistence_hours)
        flags = [False] * len(times)
        for index, day, hour, value in samples:
            key = (day, hour)
            center = centers.get(key)
            if key not in flagged_hours or center is None:
                continue
            if abs(value - center) > self._excess_c:
                flags[index] = True
        return flags


def _aligned_inputs(
    timestamps: Sequence[datetime],
    temperatures: Sequence[float],
) -> tuple[list[datetime], np.ndarray]:
    times = [_require_datetime(value) for value in timestamps]
    values = np.asarray(temperatures, dtype=float)
    if values.ndim != 1:
        raise ValueError(
            f"temperatures must be 1-D, got {values.ndim}-D"
        )
    if len(times) != int(values.shape[0]):
        raise ValueError(
            "timestamps and temperatures differ in length: "
            f"{len(times)} vs {values.shape[0]}"
        )
    return times, values


def _require_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    raise TypeError(
        "timestamps must be datetime instances, "
        f"got {type(value).__name__}"
    )


def _finite_samples(times: Sequence[datetime], values: np.ndarray) -> list[_Sample]:
    samples: list[_Sample] = []
    for index, (stamp, value) in enumerate(zip(times, values)):
        if not np.isfinite(value):
            continue
        samples.append((index, stamp.date(), stamp.hour, float(value)))
    return samples


def _hour_medians(samples: Sequence[_Sample]) -> dict[_HourKey, float]:
    buckets: dict[_HourKey, list[float]] = {}
    for _index, day, hour, value in samples:
        buckets.setdefault((day, hour), []).append(value)
    return {
        key: float(np.median(bucket))
        for key, bucket in buckets.items()
    }


def _leave_one_day_centers(
    medians: dict[_HourKey, float],
    min_other_days: int,
) -> dict[_HourKey, float]:
    """Median of other days at the same clock hour, one vote per day."""
    by_hour: dict[int, list[tuple[date, float]]] = {}
    for (day, hour), median in medians.items():
        by_hour.setdefault(hour, []).append((day, median))
    centers: dict[_HourKey, float] = {}
    for day, hour in medians:
        others = [
            value
            for other_day, value in by_hour[hour]
            if other_day != day
        ]
        if len(others) >= min_other_days:
            centers[(day, hour)] = float(np.median(others))
    return centers


def _exceeding_hours(
    medians: dict[_HourKey, float],
    centers: dict[_HourKey, float],
    excess_c: float,
) -> list[_HourKey]:
    exceeding = [
        key
        for key, median in medians.items()
        if key in centers and abs(median - centers[key]) > excess_c
    ]
    exceeding.sort(key=_hour_ordinal)
    return exceeding


def _persistent_hours(
    exceeding: Sequence[_HourKey],
    persistence_hours: int,
) -> set[_HourKey]:
    """Hours that complete a run of consecutive exceeding clock hours.

    Only exceeding hours are walked. A gap of one or more clock hours,
    including a non-exceeding hour that was never passed in, resets the
    run because the ordinals are no longer adjacent.
    """
    flagged: set[_HourKey] = set()
    run = 0
    previous: int | None = None
    for key in exceeding:
        ordinal = _hour_ordinal(key)
        if previous is not None and ordinal == previous + 1:
            run += 1
        else:
            run = 1
        previous = ordinal
        if run >= persistence_hours:
            flagged.add(key)
    return flagged


def _hour_ordinal(key: _HourKey) -> int:
    day, hour = key
    return day.toordinal() * 24 + hour

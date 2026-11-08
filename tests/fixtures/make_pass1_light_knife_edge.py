"""Generator for the two Pass-1 bimodal-light knife-edge fixtures (BACKLOG-14).

Run from the repo root to regenerate both CSVs in place::

    ./venv/bin/python tests/fixtures/make_pass1_light_knife_edge.py

Output (committed alongside this script):

* ``pass1_light_knife_edge_high_modal.csv`` — case A, mirrors the tenant_1
  corpus (43.9% of light rows at exactly 0). Modal share is **above**
  ``BOOTSTRAP_MAD_MAX_MODAL_SHARE``, so the named exclusion list *and* the
  generic degeneracy rule both independently disqualify ``light``.
* ``pass1_light_knife_edge_low_modal.csv`` — case B, mirrors
  ``native_july_clearsky.csv`` (34.9% at exactly 0). Modal share is **below**
  the limit, so only the exclusion list disqualifies ``light``. This is the
  case that would regress unnoticed if ``BOOTSTRAP_MAD_EXCLUDED_CHANNELS`` were
  emptied.

Why a synthetic file rather than a downsample of the real tenant CSV: the real
file is customer data and must not be committed, and a downsample does not
reliably preserve the exact knife edge this fixture exists to pin.

## The knife edge

``compute_mad_flags`` once took one whole-file median/MAD per channel, which
assumes a unimodal channel. With ``z`` = the fraction of light rows at exactly
0 and ``f`` = the fraction at a small floor value ``v``:

* the median lands on ``v`` iff ``z < 0.5 <= z + f``
* the MAD equals ``v`` iff ``f < 0.5 <= f + z``

Both hold whenever ``z + f >= 0.5`` with ``z < 0.5`` and ``f < 0.5``, and then
``MAD == median`` identically, so the robust-Z cull boundary collapses to

    cut = median * (1 + 3.5 / 0.6745) = 6.18903 * median

i.e. 8.3552 W/m^2 here — every daylight row is culled for being daylight.

The discontinuity is the point. **Above** 50% dark the MAD is 0, the
``mad_z_scores`` guard short-circuits and the channel culls nothing; **just
below** 50% it culls the whole other mode. A fixture on the wrong side of that
line passes for the wrong reason, which is why the counts below are exact
integers over an **odd** row count rather than approximate fractions. See the
comment on ``ROWS``: an even count makes ``numpy.median`` interpolate between
the two middle order statistics, so the median is a value no row holds and
``MAD == median`` stops holding exactly.

## Adapter constraints these fixtures are shaped around

``src/vendors/csv_adapter.py`` reshapes input before Pass 1 sees it:

* ``REQUIRED_SENSOR_COLS`` — rows with NaN temperature or humidity are
  *deleted*, so every row here carries both.
* ``SENSOR_DEFAULTS`` — an **absent** light column is filled with the constant
  500.0 (not 0) and absent co2 with 400.0, which would make either channel
  degenerate by accident. Both are therefore real columns here.
* headers must be vendor-plausible so the real ``_smart_column_map`` path runs;
  ``Light Intensity (W/m2)`` also pins ``infer_light_unit`` to W/m^2 so no unit
  conversion is applied to the carefully chosen values.
* ``BOOTSTRAP_MIN_CLEAN_ROWS`` is 100, so ``ROWS`` leaves comfortable headroom
  after IsolationForest's ~4% contamination budget.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

# --- Grid ------------------------------------------------------------------
# ROWS MUST BE ODD. This is the one constant here that cannot be changed
# casually, and the reason is `numpy.median`, not anything about greenhouses.
#
# With an even count numpy returns the **mean of the two middle order
# statistics** rather than a value present in the data. The knife edge this
# fixture exists to reproduce is the identity `MAD == median`, and that
# identity is exact only when the median lands *on* the floor value `v`:
#
#   even ROWS  -> median = (v + first_bright) / 2, some value no row holds,
#                 every deviation shifts, and MAD != median. The fixture then
#                 silently stops reproducing the bug and the regression tests
#                 pass for the wrong reason.
#   odd ROWS   -> median = the element at index (ROWS - 1) // 2, which the
#                 counts below place squarely inside the floor mode, so
#                 median == v and MAD == v exactly.
#
# 401 was chosen as the smallest odd count that leaves comfortable headroom
# over BOOTSTRAP_MIN_CLEAN_ROWS (100) after IsolationForest's ~4%
# contamination budget, while keeping the file near 18 KB.
#
# If you change ROWS, re-derive CASE_HIGH_MODAL and CASE_LOW_MODAL with it:
# both need dark + floor == (ROWS + 1) // 2 with each part under ROWS / 2.
ROWS: int = 401
INTERVAL_MINUTES: int = 5
START = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

# --- Light channel ---------------------------------------------------------
# Dark rows sit at exactly 0; the floor mode sits at LIGHT_FLOOR; the rest are
# bright and above 2 * LIGHT_FLOOR so their deviations exceed the MAD.
LIGHT_FLOOR: float = 1.35  # the tenant_1 corpus measured median(light) = 1.35
BRIGHT_MIN: float = 40.0
BRIGHT_MAX: float = 880.0

# (dark_rows, floor_rows) per case. Both satisfy dark + floor == 201 > ROWS / 2
# with dark < ROWS / 2 and floor < ROWS / 2, which is what forces
# MAD == median. Case A's dark share clears BOOTSTRAP_MAD_MAX_MODAL_SHARE
# (0.40); case B's deliberately does not.
CASE_HIGH_MODAL: tuple[int, int] = (176, 25)  # dark share 0.43890
CASE_LOW_MODAL: tuple[int, int] = (140, 61)   # dark share 0.34913

# --- Other channels --------------------------------------------------------
TEMP_BASE, TEMP_SWING = 21.0, 4.0
HUMIDITY_BASE, HUMIDITY_SWING = 65.0, 8.0
CO2_BASE, CO2_SWING = 420.0, 60.0

# A genuine wet-sensor excursion. These rows SHOULD be MAD-culled: the fix must
# not have become "skip every channel", so the suite asserts humidity keeps
# these culls. 99.0 %RH stays <= 100 so the psychrometric stage ignores it and
# the cull is attributable to the MAD stage alone.
WET_RUN_START: int = 150
WET_RUN_ROWS: int = 12
WET_HUMIDITY: float = 99.0

HEADER = (
    "Timestamp,Measured Greenhouse Temp (C),Measured Relative Humidity (%),"
    "Measured CO2 (ppm),Light Intensity (W/m2)"
)

_HIGH_MODAL_NAME = "pass1_light_knife_edge_high_modal.csv"
_LOW_MODAL_NAME = "pass1_light_knife_edge_low_modal.csv"


def _diurnal_curve() -> np.ndarray:
    """Clipped sine over time-of-day — 0 at night, peaking at local noon."""
    minutes = np.arange(ROWS) * INTERVAL_MINUTES
    day_fraction = (minutes % 1440) / 1440.0
    return np.clip(np.sin(2.0 * math.pi * (day_fraction - 0.25)), 0.0, None)


def build_light_channel(dark_rows: int, floor_rows: int) -> np.ndarray:
    """Light values with exactly *dark_rows* zeros and *floor_rows* at the floor.

    Ranking by the diurnal curve keeps the shape physically plausible — the
    darkest rows really are the night ones — while the counts stay exact.
    """
    curve = _diurnal_curve()
    darkest_first = np.argsort(curve, kind="stable")
    values = np.empty(ROWS, dtype=float)
    values[darkest_first[:dark_rows]] = 0.0
    values[darkest_first[dark_rows:dark_rows + floor_rows]] = LIGHT_FLOOR

    bright_index = darkest_first[dark_rows + floor_rows:]
    bright_curve = curve[bright_index]
    low, high = float(bright_curve.min()), float(bright_curve.max())
    span = (bright_curve - low) / (high - low) if high > low else np.zeros_like(bright_curve)
    values[bright_index] = np.round(BRIGHT_MIN + span * (BRIGHT_MAX - BRIGHT_MIN), 1)
    return values


def build_temperature_channel() -> np.ndarray:
    """Smooth diurnal temperature: unimodal, non-degenerate, no robust-Z outlier."""
    wave = _unit_wave()
    ripple = 0.15 * np.sin(np.arange(ROWS) * 0.7)
    return np.round(TEMP_BASE + TEMP_SWING * wave + ripple, 2)


def build_humidity_channel() -> np.ndarray:
    """Diurnal humidity in anti-phase with temperature, plus one wet excursion."""
    wave = _unit_wave()
    ripple = 0.3 * np.cos(np.arange(ROWS) * 0.9)
    values = np.round(HUMIDITY_BASE - HUMIDITY_SWING * wave + ripple, 2)
    values[WET_RUN_START:WET_RUN_START + WET_RUN_ROWS] = WET_HUMIDITY
    return values


def build_co2_channel() -> np.ndarray:
    """Varying co2 so the channel is really scrubbed rather than skipped."""
    minutes = np.arange(ROWS) * INTERVAL_MINUTES
    day_fraction = (minutes % 1440) / 1440.0
    return np.round(CO2_BASE + CO2_SWING * np.cos(2.0 * math.pi * day_fraction), 2)


def _unit_wave() -> np.ndarray:
    minutes = np.arange(ROWS) * INTERVAL_MINUTES
    day_fraction = (minutes % 1440) / 1440.0
    return np.sin(2.0 * math.pi * (day_fraction - 0.25))


def render_csv(dark_rows: int, floor_rows: int) -> str:
    """Render one fixture as tenant-shaped CSV text (comma sep, dot decimal, ISO-Z)."""
    light = build_light_channel(dark_rows, floor_rows)
    temperature = build_temperature_channel()
    humidity = build_humidity_channel()
    co2 = build_co2_channel()

    lines = [HEADER]
    for row in range(ROWS):
        stamp = (START + timedelta(minutes=INTERVAL_MINUTES * row)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        lines.append(
            f"{stamp},{temperature[row]:.2f},{humidity[row]:.2f},"
            f"{co2[row]:.2f},{light[row]:.2f}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    here = Path(__file__).resolve().parent
    for name, (dark_rows, floor_rows) in (
        (_HIGH_MODAL_NAME, CASE_HIGH_MODAL),
        (_LOW_MODAL_NAME, CASE_LOW_MODAL),
    ):
        text = render_csv(dark_rows, floor_rows)
        (here / name).write_text(text, encoding="utf-8")
        print(f"wrote {name}: {len(text.splitlines()) - 1} rows, {len(text)} bytes")


if __name__ == "__main__":
    main()

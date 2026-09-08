# Native Fixture Provenance Record

## Source file

- **Absolute path:** `/Users/eugenesidorov/Downloads/2026_utk_test.csv`
- **SHA-256:** `7e3aecc815d0b785f553839792c6dea63fa7813aecf5632e1d1a09d84f929e34`
- **Row count:** 105,120 data rows (105,121 lines including header)
- **Cadence verification:** All 105,119 inter-row gaps are EXACTLY 300 seconds (5-minute uniform cadence)
- **Date range:** 2026-01-01 00:00 through 2026-12-31 23:55 (365 days x 288 samples/day)
- **Format:** Semicolon-delimited, comma decimal separator (`3,1`), day-first dates (`DD.MM.YYYY HH:MM`), European SCADA export
- **Total null humidity readings:** 199 (real sensor dropouts across the year)

## WARNING: `data/*_csvtest_20260712.csv` are NOT native fixtures

The files `data/bootstrapped_clean_csvtest_20260712.csv` and
`data/pass1_suspected_anomalies_csvtest_20260712.csv` are **outputs of this repo's own
cold-start bootstrap pipeline**, not device exports. Evidence:

- `src/models/bootstrap.py::_load_and_resample` (lines 255-275) reservoir-samples at most
  10 chunks of 5,000 rows using an **unseeded** `random.randint` (line 273), making the
  output unreproducible.
- The pipeline then calls `.resample("5min").mean()` (line 302) and
  `.interpolate(method="linear", limit=3)` (line 308), meaning approximately 24% of values
  are chunk means rather than measurements.
- These files have 9,057 usable rows with irregular cadence.

**These files MUST NOT be used as test fixtures.** Use only the native export fixtures below.

---

## Fixture A: `native_dec_stuck_fault.csv`

- **SHA-256:** `67476d349321770a5f253a44f9fead0a5c2729dd6f7e29eabc727042907d3deb`
- **Slice boundaries:** 2026-12-14T00:00:00 through 2026-12-26T00:00:00 (inclusive both ends)
- **Row count:** 3,457 data rows (3,458 lines including header)
- **Cadence:** All 3,456 inter-row gaps are EXACTLY 300 seconds
- **Null humidity readings:** 70 (real sensor dropouts, all within the main fault window)

### Main stuck-fault (measured ground truth)

| Metric | Measured value |
|--------|---------------|
| Fault onset timestamp | 2026-12-18T13:00:00 |
| Fault onset value | 100.0 |
| Last 100.0 timestamp | 2026-12-25T08:50:00 |
| Release timestamp | 2026-12-25T08:55:00 |
| Release value | 91.24 |
| Samples at exactly 100.0 in fault window | 1,897 |
| NaN samples in fault window | 70 |
| Non-100, non-NaN samples in fault window | 0 |
| Total samples in fault window (onset to release, exclusive) | 1,967 |
| Wall-clock duration (onset to last 100.0) | 163.83 h |

All non-NaN humidity readings in the fault window are exactly 100.0. The 70 NaN readings
are sensor dropouts interspersed within the fault period (the sensor was offline, not
reading a different value).

### Pre-onset readings (measured)

| Timestamp | Humidity |
|-----------|----------|
| 2026-12-18 12:35 | 99.17 |
| 2026-12-18 12:40 | 99.00 |
| 2026-12-18 12:45 | 99.00 |
| 2026-12-18 12:50 | 99.00 |
| 2026-12-18 12:55 | 99.83 |

These confirm the fault onset is genuine (sensor was reading near-saturation values, then
locked to exactly 100.0 at 13:00).

### Prior freezes at 100.0 within the fixture window (measured)

| # | Start | End | Samples | Duration |
|---|-------|-----|---------|----------|
| 1 | 2026-12-14 00:00 (truncated) | 2026-12-15 10:50 | 419 | 34.83 h |
| 2 | 2026-12-15 11:05 | 2026-12-16 09:10 | 266 | 22.08 h |
| 3 | 2026-12-16 11:20 | 2026-12-18 11:15 | 576 | 47.92 h |
| 4 | 2026-12-18 11:30 | 2026-12-18 12:30 | 13 | 1.00 h |

**Note:** Run #1 starts at the fixture window boundary (Dec 14 00:00). The actual run in
the source file begins at 2026-12-13 11:50 (full duration: 47.00 h). The 34.83 h value
is the truncated portion visible in the fixture.

### Discrepancy with task prompt

The task prompt stated "three shorter prior freezes at approximately 22 h, 29.5 h, 48.7 h".
Measured values are **22.08 h, 34.83 h (truncated; full 47.00 h), 47.92 h**, plus a short
1.00 h freeze. The 29.5 h value from the prompt does not match any measured freeze duration.
The 48.7 h value is close to the measured 47.92 h but not exact. **These measurements are
from the data; the prompt values are approximations that do not precisely match.**

---

## Fixture B: `native_july_clearsky.csv`

- **SHA-256:** `cb7b3a168e7eaac4768d53a796ff4447e324379578c4d41fe61ade87edb8def4`
- **Slice boundaries:** 2026-07-01T00:00:00 through 2026-07-07T23:55:00 (inclusive both ends)
- **Row count:** 2,016 data rows (2,017 lines including header)
- **Cadence:** All 2,015 inter-row gaps are EXACTLY 300 seconds
- **Null humidity readings:** 0 (no sensor dropouts in this window)

### Clear-sky light measurements (measured ground truth)

| Metric | Measured value |
|--------|---------------|
| Maximum light intensity | 1019.98 W/m2 |
| Max light timestamp | 2026-07-02 12:25 |

This window was selected as the 7-day July window with the highest sum of daily midday
light peaks. Daily maximums:

| Date | Max light (W/m2) |
|------|-----------------|
| 2026-07-01 | 978.04 |
| 2026-07-02 | 1019.98 |
| 2026-07-03 | 1009.07 |
| 2026-07-04 | 908.82 |
| 2026-07-05 | 961.21 |
| 2026-07-06 | 978.43 |
| 2026-07-07 | 896.58 |

The task prompt mentioned "midday light readings near 983 W/m2". The actual maximum is
1019.98 W/m2 and several days have peaks near or above 983. This fixture confirms that
normal clear-sky irradiance (up to ~1020 W/m2) should never raise a light alert.

# `native_dec_stuck_fault.csv` — verified stuck-fault baseline

Source of truth for the numbers below: `tests/fixtures/NATIVE_FIXTURES.md`
("Prior freezes at 100.0 within the fixture window (measured)" and "Main
stuck-fault (measured ground truth)"). Those values were measured directly from
the fixture, independently of the detector implementation.

Fixture shape: 3457 data rows, `;`-delimited, 222209 bytes, spanning
`14.12.2026 00:00` through `26.12.2026 00:00` at 5-minute cadence.

## The five humidity freeze runs at exactly 100.0

| # | Onset | Duration | Alerts? |
|---|-------|----------|---------|
| 1 | 2026-12-14 00:00 (truncated at slice boundary) | 34.83 h | YES |
| 2 | 2026-12-15 11:05 | 22.08 h | YES |
| 3 | 2026-12-16 11:20 | 47.92 h | YES |
| 4 | 2026-12-18 11:30 | 1.00 h | **NO** — under the 6 h gate |
| main | 2026-12-18 13:00 | 163.83 h | YES |

**Expected result: exactly 4 alerts, onsets 2026-12-14 / 12-15 / 12-16 / 12-18 13:00.**

Run #1's 34.83 h is a truncation artifact, not the true freeze length: the freeze
begins before the fixture's slice boundary. In the unsliced source file it starts
at 2026-12-13 11:50 for a full 47.00 h. Truncated or not, it clears the 6 h gate,
so the alert count is unaffected.

## Why "4 alerts" and "+6.00 h" are one fact, not two

These two expectations are often quoted side by side as if they were independent
checks. They are not — they are the same fact stated twice, and that is precisely
what makes the pair load-bearing.

The detector fires once a freeze has persisted for the 6 h wall-clock gate, and
stamps `detected_ts = onset + 6 h`. That single rule produces both numbers:

- It is why `detected_ts - onset` is exactly 6.00 h on **every** alert. The delta
  is not data-dependent; any value other than 6.00 h means the gate itself moved.
- It is why there are **4** alerts and not 5. Run #4 lasts 1.00 h and never
  reaches the gate, so it is correctly silent.

So a run reporting 5 alerts, or a delta other than 6.00 h, points at the same
place: the 6 h gate. Checking the count without the delta (or vice versa) leaves
that failure mode half-covered. Run #4 is the discriminating case — it is the only
freeze whose presence or absence distinguishes a working gate from an absent one.

## The 70 dropped rows are expected, not data loss

Uploading this fixture persists **3387** rows, not 3457. The 70-row difference is
ground truth, not a defect: `NATIVE_FIXTURES.md` records 70 NaN humidity readings
inside the fault window, sensor dropouts where the sensor was offline rather than
reading a different value. Inspecting the CSV directly confirms exactly 70 rows
carry no usable sensor data (66 fully blank across every sensor column, plus 4
where only `Rain` is populated), all falling between 19.12 15:00 and 22.12 09:20.

3457 − 70 = 3387, and 3387 − 19 = 3368 sequences at `seq_len=20`. Both the local
sync path and the production async path report `sequences_processed = 3368`, so
both drop the same rows. A future reviewer seeing "3387 of 3457" should not treat
it as silent row loss.

## The 102 non-humidity statistical figure is UNCORROBORATED

`102` total statistical hits / `0` humidity / `102` non-humidity is **not** fixture
ground truth. `NATIVE_FIXTURES.md` makes no claim about statistical hit counts at
all. Every occurrence of the number traces to code and docs written by the branch
under test:

- `docs/runbooks/deploy.md:137` — "**102 non-humidity** statistical hits"
- `tests/workers/test_stat_chunk_invariance.py:70-72` — `EXPECTED_STAT_TOTAL = 102`,
  `EXPECTED_STAT_HUMIDITY = 0`, `EXPECTED_STAT_NON_HUMIDITY = 102`

The figure is *reproducible* — it recomputes to 102 at every chunk size and on both
the sync and async paths — but reproducibility here is self-referential: it confirms
the implementation is internally consistent, not that 102 is the right answer. The
same file records that `102` already superseded an earlier pinned `111` after task
1d-2 changed baseline exclusion, which is a concrete demonstration that this number
tracks implementation behaviour rather than a property of the fixture.

Treat a deviation from 102 as a prompt to find out where 102 came from, not as
proof of a regression. The per-column breakdown, for whoever needs to diff it:

| Column | Hits |
|--------|------|
| `calculated_heating_temp` | 27 |
| `measured_hd` | 23 |
| `measured_pipe_1_temp` | 23 |
| `measured_pipe_2_temp` | 26 |
| `measured_plant_temp` | 3 |

## Counting statistical hits: watch the compound detector label

When a row trips both the LSTM and the statistical detector, its `detector` field
becomes `"lstm+statistical"`, not `"statistical"`. Filtering with
`detector == "statistical"` therefore undercounts.

On a production run with a real (non-mock) model this silently returned 99 instead
of 102 — the three missing entries were `calculated_heating_temp` at 2026-12-25
11:20 / 11:40 / 11:50, all carrying the compound label. A local reference run using
a constant-score mock detector trips no LSTM, so every hit is labelled plain
`"statistical"` and the discrepancy does not appear locally.

Use a substring test (`"statistical" in detector`) when comparing hit sets across
runs, or a mock-vs-real comparison will look like a carry-path regression when
nothing is wrong.
